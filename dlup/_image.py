# Copyright (c) dlup contributors
"""Whole slide image access objects.

In this module we take care of abstracting the access to whole slide images.
The main workhorse is SlideImage which takes care of simplifying region extraction
of discrete-levels pyramidal images in a continuous way, supporting multiple different backends.
"""
from __future__ import annotations

import errno
import io
import os
import pathlib
import warnings
from enum import Enum
from types import TracebackType
from typing import Any, Literal, Optional, Type, TypeVar, cast

import numpy as np
import numpy.typing as npt
import PIL
import PIL.Image
import PIL.ImageCms
import pyvips
from pyvips.enums import Kernel as VipsKernel

from dlup._exceptions import UnsupportedSlideError
from dlup._region import BoundaryMode, RegionView
from dlup._types import GenericFloatArray, GenericIntArray, GenericNumber, GenericNumberArray, PathLike
from dlup.backends.common import AbstractSlideBackend
from dlup.utils.backends import ImageBackend
from dlup.utils.image import check_if_mpp_is_valid

_Box = tuple[GenericNumber, GenericNumber, GenericNumber, GenericNumber]
_TSlideImage = TypeVar("_TSlideImage", bound="SlideImage")


class Resampling(str, Enum):
    """Resampling methods SlideImage (e.g. for images or masks)."""

    NEAREST = "NEAREST"
    LANCZOS = "LANCZOS"


_RESAMPLE_TO_PIL = {Resampling.NEAREST: PIL.Image.Resampling.NEAREST, Resampling.LANCZOS: PIL.Image.Resampling.LANCZOS}
_RESAMPLE_TO_VIPS = {Resampling.NEAREST: VipsKernel.NEAREST, Resampling.LANCZOS: VipsKernel.LANCZOS3}


class _SlideImageRegionView(RegionView):
    """Represents an image view tied to a slide image."""

    def __init__(
        self,
        wsi: _TSlideImage,
        scaling: GenericNumber,
        boundary_mode: BoundaryMode | None = None,
    ):
        """Initialize with a slide image object and the scaling level."""
        # Always call the parent init
        super().__init__(boundary_mode=boundary_mode)
        self._wsi = wsi
        self._scaling = scaling

    @property
    def mpp(self) -> float:
        """Returns the level effective mpp."""
        return self._wsi.mpp / self._scaling

    @property
    def size(self) -> tuple[int, int]:
        """Size"""
        return self._wsi.get_scaled_size(self._scaling)

    def _read_region_impl(self, location: GenericFloatArray, size: GenericIntArray) -> pyvips.Image:
        """Returns a region of the level associated to the view."""
        x, y = location
        w, h = size
        return self._wsi.read_region((x, y), self._scaling, (w, h))


def _clip2size(
    a: npt.NDArray[np.int_ | np.float_], size: tuple[GenericNumber, GenericNumber]
) -> npt.NDArray[np.int_ | np.float_]:
    """Clip values from 0 to size boundaries."""
    return np.clip(a, (0, 0), size)


def _check_size_and_location(
    size: npt.NDArray[np.int_ | np.float_],
    location: npt.NDArray[np.int_ | np.float_],
    level_size: npt.NDArray[np.int_],
) -> None:
    """
    Check if the size and location are within bounds for the given level size.

    Parameters
    ----------
    size : npt.NDArray[np.int_ | np.float_]
        The size of the region to extract.
    location : npt.NDArray[np.int_ | np.float_]
        The location of the region to extract.
    level_size : npt.NDArray[np.int_]
        The size of the level.

    Raises
    ------
    ValueError
        If the size is negative or if the location is outside the level boundaries.

    Returns
    -------
    None
    """
    if (size < 0).any():
        raise ValueError(f"Size values must be greater than zero. Got {size}")

    if ((location < 0) | ((location + size) > level_size)).any():
        raise ValueError(
            f"Requested region is outside level boundaries. "
            f"{location.tolist()} + {size.tolist()} (={(location + size).tolist()}) > {level_size.tolist()}."
        )


class SlideImage:
    """Utility class to simplify whole-slide pyramidal images management.

    This helper class furtherly abstracts openslide access to WSIs
    by validating some of the properties and giving access
    to a continuous pyramid. Layer values are interpolated from
    the closest high resolution layer.
    Each horizontal slices of the pyramid can be accessed using a scaling value
    z as index.

    Lifetime
    --------
    SlideImage is currently initialized and holds an openslide image object.
    The openslide wsi instance is automatically closed when gargbage collected.

    Examples
    --------
    >>> import dlup
    >>> wsi = dlup.SlideImage.from_file_path('path/to/slide.svs')
    """

    def __init__(
        self,
        wsi: AbstractSlideBackend,
        identifier: str | None = None,
        interpolator: Optional[Resampling] | str = Resampling.LANCZOS,
        overwrite_mpp: Optional[tuple[float, float]] = None,
        apply_color_profile: bool = False,
        internal_handler: Optional[Literal["pil", "vips"]] = None,
    ) -> None:
        """Initialize a whole slide image and validate its properties. This class allows to read whole-slide images
        at any arbitrary resolution. This class can read images from any backend that implements the
        AbstractSlideBackend interface.

        Parameters
        ----------
        wsi : AbstractSlideBackend
            The slide object.
        identifier : str, optional
            A user-defined identifier for the slide, used in e.g. exceptions.
        interpolator : Resampling, optional
            The interpolator to use when reading regions. For images typically LANCZOS is the best choice. Masks
            can use NEAREST. By default, will use LANCZOS
        overwrite_mpp : tuple[float, float], optional
            Overwrite the mpp of the slide. For instance, if the mpp is not available, or when sourcing from
            and external database.
        apply_color_profile : bool
            Whether to apply the color profile to the output regions.
        internal_handler : Literal["pil", "vips"], optional
            The internal handler to use for processing the regions. This can be either PIL or VIPS. PIL is the behavior
            for all dlup versions prior to v0.4. It is recommended to migrate your code and use VIPS instead.

        Raises
        ------
        UnsupportedSlideError
            If the slide is not supported, or when the mpp is not valid (too anisotropic).

        Returns
        -------
        None

        """
        self._wsi = wsi
        self._identifier = identifier

        self._interpolator = interpolator if interpolator else Resampling.LANCZOS

        if overwrite_mpp is not None:
            self._wsi.spacing = overwrite_mpp

        if self._wsi.spacing is None:
            raise UnsupportedSlideError(
                f"The spacing of {identifier} cannot be derived from image and is "
                "not explicitly set in the `overwrite_mpp` parameter."
            )

        check_if_mpp_is_valid(*self._wsi.spacing)
        self._avg_native_mpp = (float(self._wsi.spacing[0]) + float(self._wsi.spacing[1])) / 2

        self._apply_color_profile = apply_color_profile
        self.__color_transforms = None

        if not internal_handler:
            warnings.warn(
                "The internal handler is not set. Defaulting to PIL. "
                "This behavior will be changed in dlup v1.0 where the default handler will become vips. "
                'If you want your code to keep working as before you will need to set internal_handler="pil".',
                UserWarning,
            )

        self._internal_handler = internal_handler if internal_handler is not None else "pil"
        self.__color_transform: PIL.ImageCms.ImageCmsTransform | None = None

    @property
    def internal_handler(self) -> Literal["pil", "vips"]:
        """Returns the internal handler used for processing the regions."""
        return self._internal_handler

    @property
    def interpolator(self) -> Resampling:
        """Returns the interpolator used for processing the regions."""
        return self._interpolator if isinstance(self._interpolator, Resampling) else Resampling[self._interpolator]

    def close(self) -> None:
        """Close the underlying image."""
        self._wsi.close()

    @property
    def color_profile(self) -> io.BytesIO | None:
        """Returns the ICC profile of the image.
        Each image in the pyramid has the same ICC profile, but the associated images might have their own.

        # TODO: Vips can apply the color profile directly when loading the image!

        Examples
        --------
        >>> import dlup
        >>> from PIL import ImageCms
        >>> wsi = dlup.SlideImage.from_file_path("path/to/slide.svs")
        >>> region = wsi.read_region((0, 0), 1.0, (512, 512))
        >>> to_profile = ImageCms.createProfile("sRGB")
        >>> color_profile = PIL.ImageCms.getOpenProfile(wsi.color_profile)
        >>> intent = ImageCms.getDefaultIntent(color_profile)
        >>> transform = ImageCms.buildTransform(color_profile, to_profile, "RGBA", "RGBA", intent, 0)
        >>> # Now you can apply the transform to the region (or any other PIL image)
        >>> ImageCms.applyTransform(region, transform, True)

        Returns
        -------
        io.BytesIO
            The ICC profile of the image.
        """
        return getattr(self._wsi, "color_profile", None)

    def __enter__(self) -> "SlideImage":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> Literal[False]:
        self.close()
        return False

    @classmethod
    def from_file_path(
        cls: Type[_TSlideImage],
        wsi_file_path: PathLike,
        identifier: str | None = None,
        backend: ImageBackend | Type[AbstractSlideBackend] | str = ImageBackend.OPENSLIDE,
        **kwargs: Any,
    ) -> _TSlideImage:
        wsi_file_path = pathlib.Path(wsi_file_path).resolve()
        if not wsi_file_path.exists():
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(wsi_file_path))

        if isinstance(backend, str):
            backend = ImageBackend[backend]

        # Adjust how the backend is used depending on its type
        if isinstance(backend, ImageBackend):
            backend_callable = backend.value  # Get the callable from Enum
        elif issubclass(backend, AbstractSlideBackend):
            backend_callable = backend  # Directly use the class if it's a subclass of AbstractSlideBackend
        else:
            raise TypeError("backend must be either an ImageBackend enum or a subclass of AbstractSlideBackend")

        try:
            wsi = backend_callable(wsi_file_path)  # Instantiate the backend with the path
        except UnsupportedSlideError as exc:
            raise UnsupportedSlideError(f"Unsupported file: {wsi_file_path}") from exc

        return cls(wsi, identifier if identifier is not None else str(wsi_file_path), **kwargs)

    def read_region(
        self,
        location: GenericNumberArray | tuple[GenericNumber, GenericNumber],
        scaling: float,
        size: npt.NDArray[np.int_] | tuple[int, int],
    ) -> PIL.Image.Image | pyvips.Image:
        """Return a region at a specific scaling level of the pyramid.

        A typical slide is made of several levels at different mpps.
        In normal cirmustances, it's not possible to retrieve an image of
        intermediate mpp between these levels. This method takes care of
        subsampling the closest high resolution level to extract a target
        region via interpolation.

        Once the best layer is selected, a native resolution region
        is extracted, with enough padding to include the samples necessary to downsample
        the final region (considering LANCZOS interpolation method basis functions).

        The steps are approximately the following:

        1. Map the region that we want to extract to the below layer.
        2. Add some extra values (left and right) to the native region we want to extract
           to take into account the interpolation samples at the border ("native_extra_pixels").
        3. Map the location to the level0 coordinates, floor it to add extra information
           on the left (level_zero_location_adapted).
        4. Re-map the integral level-0 location to the native_level.
        5. Compute the right bound of the region adding the native_size and extra pixels (native_size_adapted).
           The size is also clipped so that any extra pixel will fit within the native level.
        6. Since the native_size_adapted needs to be passed to openslide and has to be an integer, we ceil it
           to avoid problems with possible overflows of the right boundary of the target region being greater
           than the right boundary of the sample region
           (native_location + native_size > native_size_adapted + native_location_adapted).
        7. Crop the target region from within the sampled region by computing the relative
           coordinates (fractional_coordinates).

        Parameters
        ----------
        location : tuple[int, int]
            Location from the top left (x, y) in pixel coordinates given at the requested scaling.
        scaling : float
            The scaling to be applied compared to level 0.
        size : tuple[int, int]
            Region size of the resulting region.

        Returns
        -------
        PIL.Image
            The extract region.

        Examples
        --------
        The locations are defined at the requested scaling (with respect to level 0), so if we want to extract at
        location ``(location_x, location_y)`` of a scaling 0.5 (with respect to level 0), and have
        resulting tile size of ``(tile_size, tile_size)`` with a scaling factor of 0.5, we can use:
        >>>  wsi.read_region(location=(coordinate_x, coordinate_y), scaling=0.5, size=(tile_size, tile_size))
        """
        wsi = self._wsi
        location = np.asarray(location)
        size = np.asarray(size)
        level_size = np.array(self.get_scaled_size(scaling))

        _check_size_and_location(location, size, level_size)

        native_level = wsi.get_best_level_for_downsample(1 / scaling)
        native_level_size = wsi.level_dimensions[native_level]
        native_level_downsample = wsi.level_downsamples[native_level]
        native_scaling = scaling * wsi.level_downsamples[native_level]
        native_location = location / native_scaling
        native_size = size / native_scaling

        # OpenSlide doesn't feature float coordinates to extract a region.
        # We need to extract enough pixels and let PIL do the interpolation.
        # In the borders, the basis functions of other samples contribute to the final value.
        # PIL lanczos uses 3 pixels as support.
        # See pillow: https://git.io/JG0QD
        native_extra_pixels = 3 if native_scaling > 1 else np.ceil(3 / native_scaling)

        # Compute the native location while counting the extra pixels.
        native_location_adapted = np.floor(native_location - native_extra_pixels).astype(int)
        native_location_adapted = _clip2size(native_location_adapted, native_level_size)

        # Unfortunately openslide requires the location in pixels from level 0.
        level_zero_location_adapted = np.floor(native_location_adapted * native_level_downsample).astype(int)
        native_location_adapted = level_zero_location_adapted / native_level_downsample
        native_size_adapted = np.ceil(native_location + native_size + native_extra_pixels).astype(int)
        native_size_adapted = _clip2size(native_size_adapted, native_level_size) - native_location_adapted

        # By casting to int we introduce a small error in the right boundary leading
        # to a smaller region which might lead to the target region to overflow from the sampled
        # region.
        native_size_adapted = np.ceil(native_size_adapted).astype(int)

        # We extract the region via the slide backend with the required extra border
        vips_region = wsi.read_region(
            (level_zero_location_adapted[0], level_zero_location_adapted[1]),
            native_level,
            (native_size_adapted[0], native_size_adapted[1]),
        )

        fractional_coordinates = native_location - native_location_adapted
        size = cast(tuple[int, int], size)

        if self._internal_handler == "pil":
            box = (
                *fractional_coordinates,
                *np.clip(
                    (fractional_coordinates + native_size),
                    a_min=0,
                    a_max=(vips_region.width, vips_region.height),
                ),
            )
            box = cast(tuple[float, float, float, float], box)

            # Within this region, there are a bunch of extra pixels, we interpolate to sample
            # the pixel in the right position to retain the right sample weight.
            # We also need to clip to the border, as some readers (e.g mirax) have one pixel less at the border.

            pil_region = PIL.Image.fromarray(np.asarray(vips_region)).resize(
                size,
                resample=_RESAMPLE_TO_PIL[self.interpolator],
                box=box,
            )

            if self._apply_color_profile:
                warnings.warn("Applying color profile is not supported with PIL backend.", UserWarning)

            return pyvips.Image.new_from_array(np.asarray(pil_region), interpretation=vips_region.interpretation)

        if self._internal_handler == "vips":
            crop_box = (
                int(np.floor(fractional_coordinates[0])),
                int(np.floor(fractional_coordinates[1])),
                int(np.round(fractional_coordinates[0] + native_size[0])),
                int(np.round(fractional_coordinates[1] + native_size[1])),
            )

            # Crop the region
            crop_region = vips_region.crop(
                crop_box[0], crop_box[1], crop_box[2] - crop_box[0], crop_box[3] - crop_box[1]
            )

            if self._apply_color_profile and self.color_profile is not None:
                crop_region.set_type(pyvips.GValue.blob_type, "icc-profile-data", self.color_profile.read())
                crop_region = crop_region.icc_transform("srgb")
            # Calculate the size of the target region
            target_width, target_height = size

            # Resize the cropped region to the target size
            resized_region = crop_region.resize(
                target_width / crop_region.width,
                vscale=target_height / crop_region.height,
                kernel=_RESAMPLE_TO_VIPS[self.interpolator],
            )

            return resized_region

    def get_scaled_size(self, scaling: GenericNumber, limit_bounds: Optional[bool] = False) -> tuple[int, int]:
        """Compute slide image size at specific scaling.

        Parameters
        -----------
        scaling: GenericNumber
            The factor by which the image needs to be scaled.

        limit_bounds: Optional[bool]
            If True, the scaled size will be calculated using the slide bounds of the whole slide image.
            This is generally the specific area within a whole slide image where we can find the tissue specimen.

        Returns
        -------
        size: tuple[int, int]
            The scaled size of the image.
        """
        if limit_bounds:
            _, bounded_size = self.slide_bounds
            size = int(bounded_size[0] * scaling), int(bounded_size[1] * scaling)
        else:
            size = int(self.size[0] * scaling), int(self.size[1] * scaling)
        return size

    def get_mpp(self, scaling: float) -> float:
        """Returns the respective mpp from the scaling."""
        return self._avg_native_mpp / scaling

    def get_closest_native_level(self, mpp: float) -> int:
        """Returns the closest native level to the given mpp.

        Returns
        -------
        int
            The closest level.
        """
        closest_index, _ = min(enumerate(self._wsi.level_spacings), key=lambda x: abs((x[1][0] + x[1][1]) / 2 - mpp))
        return closest_index

    def get_closest_native_mpp(self, mpp: float) -> tuple[float, float]:
        """Returns the closest native mpp to the given mpp.

        Returns
        -------
        tuple[float, float]
            The closest mpp in the format (mpp_x, mpp_y).
        """
        return self._wsi.level_spacings[self.get_closest_native_level(mpp)]

    def get_scaling(self, mpp: float | None) -> float:
        """Inverse of get_mpp()."""
        if not mpp:
            return 1.0
        return self._avg_native_mpp / mpp

    def get_scaled_view(self, scaling: GenericNumber) -> _SlideImageRegionView:
        """Returns a RegionView at a specific level."""
        return _SlideImageRegionView(self, scaling)

    def get_thumbnail(self, size: tuple[int, int] = (512, 512)) -> pyvips.Image:
        """Returns an RGB `PIL.Image.Image` thumbnail for the current slide.

        Parameters
        ----------
        size : tuple[int, int]
            Maximum bounding box for the thumbnail expressed as (width, height).

        Returns
        -------
        PIL.Image.Image
            The thumbnail as a PIL image.
        """
        return self._wsi.get_thumbnail(size)

    @property
    def thumbnail(self) -> pyvips.Image:
        """Returns the thumbnail with a bounding box of (512, 512)."""
        return self.get_thumbnail()

    @property
    def identifier(self) -> str | None:
        """Returns a user-defined identifier."""
        return self._identifier

    @property
    def properties(self) -> dict[str, str | int | float | bool] | None:
        """Returns any extra associated properties with the image."""
        return self._wsi.properties

    @property
    def vendor(self) -> str | None:
        """Returns the scanner vendor."""
        return self._wsi.vendor

    @property
    def size(self) -> tuple[int, int]:
        """Returns the highest resolution image size in pixels. Returns in (width, height)."""
        return self._wsi.dimensions

    @property
    def mpp(self) -> float:
        """Returns the microns per pixel of the high res image."""
        return self._avg_native_mpp

    @property
    def magnification(self) -> float | None:
        """Returns the objective power at which the WSI was sampled."""
        return self._wsi.magnification

    @property
    def aspect_ratio(self) -> float:
        """Returns width / height."""
        width, height = self.size
        return width / height

    @property
    def slide_bounds(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """Returns the bounds of the slide. These can be smaller than the image itself.
        These bounds are in the format (x, y), (width, height), and are defined at level 0 of the image.
        """
        return self._wsi.slide_bounds

    def get_scaled_slide_bounds(self, scaling: float) -> tuple[tuple[int, int], tuple[int, int]]:
        """Returns the bounds of the slide at a specific scaling level. This takes the slide bounds into account
        and scales them to the appropriate scaling level.

        Parameters
        ----------
        scaling : float
            The scaling level to use.

        Returns
        -------
        tuple[tuple[int, int], tuple[int, int]]
            The slide bounds at the given scaling level.
        """
        offset, size = self.slide_bounds
        offset = (int(scaling * offset[0]), int(scaling * offset[1]))
        size = (int(scaling * size[0]), int(scaling * size[1]))
        return offset, size

    def __repr__(self) -> str:
        """Returns the SlideImage representation and some of its properties."""
        props = ("identifier", "vendor", "mpp", "magnification", "size", "internal_handler", "interpolator", "backend")
        props_str = []
        for key in props:
            if key == "backend":
                value = self._wsi.__class__.__name__
            else:
                value = getattr(self, key, "N/A")
            props_str.append(f"{key}={value}")
        return f"{self.__class__.__name__}({', '.join(props_str)})"
