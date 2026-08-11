from __future__ import annotations

import os
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from enum import Enum
import logging
from pathlib import Path
from typing import Sequence
import warnings

from .geodesy import (
    inverse_distance_bearing,
    transpose_wgs84_enu_points,
)
from .kml_export import (
    KmlCoordinate,
    KmlDocument,
    KmlLineString,
    KmlPlacemark,
    KmlStyle,
    export_kml,
)
from .kml_file_handling import KmlTrack, parse_kml_track
from .map_preview import PreparedTrace
from .runway_alignment import RunwayReference


MAX_OUTPUT_COMPONENT_LENGTH = 96
MAX_OUTPUT_STEM_LENGTH = 200
TRANSPOSITION_FALLBACK_LINE_COLOUR = "aa00ffff"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TranspositionJob:
    """One input and its planned output within a transposition batch."""

    input_path: Path
    output_path: Path
    aircraft_name: str
    aircraft_slug: str
    target_airfield_slug: str
    overwrite_existing: bool = False
    source_runway: RunwayReference | None = None


@dataclass(frozen=True, slots=True)
class TranspositionPlan:
    """An ordered, immutable set of transposition jobs."""

    output_directory: Path
    jobs: tuple[TranspositionJob, ...]


@dataclass(frozen=True, slots=True)
class TranspositionOutput:
    """A successfully written output, including any runtime collision rename."""

    input_path: Path
    output_path: Path


class TranspositionErrorCode(str, Enum):
    """Stable categories for expected per-file transposition failures."""

    INPUT_KML = "input_kml"
    TRANSFORMATION = "transformation"
    OUTPUT_COLLISION = "output_collision"
    FILESYSTEM_WRITE = "filesystem_write"


@dataclass(frozen=True, slots=True)
class TranspositionError:
    """A safe failure description for one planned input."""

    code: TranspositionErrorCode
    message: str
    input_path: Path
    intended_output_path: Path
    exception_type: str | None = None


class TranspositionFileStatus(str, Enum):
    """Execution status for one planned input."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TranspositionFileOutcome:
    """The result of attempting exactly one transposition job."""

    input_path: Path
    planned_output_path: Path
    final_output_path: Path | None
    status: TranspositionFileStatus
    error: TranspositionError | None = None
    warnings: tuple[str, ...] = ()

    @property
    def output_path(self) -> Path:
        """Return the written output path; valid only for a successful outcome."""
        if self.final_output_path is None:
            raise ValueError("Failed transposition outcomes have no output path.")
        return self.final_output_path


@dataclass(frozen=True, slots=True)
class TranspositionBatchResult:
    """Ordered outcomes for every job in a completed transposition batch."""

    outcomes: tuple[TranspositionFileOutcome, ...]

    @property
    def successful(self) -> tuple[TranspositionFileOutcome, ...]:
        return tuple(
            outcome for outcome in self.outcomes
            if outcome.status is TranspositionFileStatus.SUCCEEDED
        )

    @property
    def failed_outcomes(self) -> tuple[TranspositionFileOutcome, ...]:
        return tuple(
            outcome for outcome in self.outcomes
            if outcome.status is TranspositionFileStatus.FAILED
        )

    @property
    def total_count(self) -> int:
        return len(self.outcomes)

    @property
    def success_count(self) -> int:
        return len(self.successful)

    @property
    def failure_count(self) -> int:
        return len(self.failed_outcomes)

    @property
    def succeeded(self) -> bool:
        return self.total_count > 0 and self.failure_count == 0

    @property
    def partially_succeeded(self) -> bool:
        return self.success_count > 0 and self.failure_count > 0

    @property
    def failed(self) -> bool:
        return self.total_count > 0 and self.success_count == 0


@dataclass(frozen=True, slots=True)
class PreparedTranspositionFile:
    """One successfully prepared input, ready for preview or later export."""

    input_path: Path
    aircraft_name: str
    trace: PreparedTrace
    warnings: tuple[str, ...] = ()

    @property
    def document(self) -> KmlDocument:
        """Return the exact quantized document used by preview and export."""
        return self.trace.adjusted_document


@dataclass(frozen=True, slots=True)
class PreparedTranspositionFailure:
    """A safe per-input preparation failure that does not assume an output path."""

    input_path: Path
    code: TranspositionErrorCode
    message: str
    exception_type: str | None = None


PreparedTranspositionItem = (
    PreparedTranspositionFile | PreparedTranspositionFailure
)


@dataclass(frozen=True, slots=True)
class PreparedTranspositionBatch:
    """Ordered in-memory preparation results for a transposition batch."""

    target_runway: RunwayReference
    items: tuple[PreparedTranspositionItem, ...]

    @property
    def prepared(self) -> tuple[PreparedTranspositionFile, ...]:
        return tuple(
            item for item in self.items
            if isinstance(item, PreparedTranspositionFile)
        )

    @property
    def failed_items(self) -> tuple[PreparedTranspositionFailure, ...]:
        return tuple(
            item for item in self.items
            if isinstance(item, PreparedTranspositionFailure)
        )

    @property
    def total_count(self) -> int:
        return len(self.items)

    @property
    def prepared_count(self) -> int:
        return len(self.prepared)

    @property
    def failure_count(self) -> int:
        return len(self.failed_items)


def _slug_component(value: str, fallback: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode(
        "ascii", "ignore"
    ).decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    return slug[:MAX_OUTPUT_COMPONENT_LENGTH].rstrip("-") or fallback


def _output_stem(
    aircraft_slug: str,
    target_airfield_slug: str,
    sequence: int,
) -> str:
    suffix = "" if sequence == 1 else f"-{sequence}"
    separator = "-at-"
    max_aircraft_length = (
        MAX_OUTPUT_STEM_LENGTH
        - len(separator)
        - len(target_airfield_slug)
        - len(suffix)
    )
    aircraft = aircraft_slug[:max_aircraft_length].rstrip("-") or "aircraft"
    return f"{aircraft}{separator}{target_airfield_slug}{suffix}"


def _next_available_output_path(
    output_directory: Path,
    aircraft_slug: str,
    target_airfield_slug: str,
    occupied_names: set[str],
) -> Path:
    sequence = 1
    while True:
        filename = f"{_output_stem(aircraft_slug, target_airfield_slug, sequence)}.kml"
        if filename.casefold() not in occupied_names:
            return output_directory / filename
        sequence += 1


def create_transposition_plan(
    input_files: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...],
    output_directory: str | os.PathLike[str],
    target_airfield: str,
) -> TranspositionPlan:
    """Plan one collision-free output per KML input without writing files."""
    if not input_files:
        raise ValueError("At least one input KML file is required.")

    output_dir = Path(output_directory)
    if not output_dir.is_dir():
        raise ValueError(f'Output directory does not exist: "{output_dir}".')

    occupied_names = {entry.name.casefold() for entry in output_dir.iterdir()}
    target_slug = _slug_component(target_airfield, "airfield")
    jobs: list[TranspositionJob] = []

    for input_file in input_files:
        input_path = Path(input_file)
        if input_path.suffix.lower() != ".kml":
            raise ValueError(f'{input_path.name}: expected a KML file.')

        aircraft_name = input_path.stem
        aircraft_slug = _slug_component(aircraft_name, "aircraft")
        output_path = _next_available_output_path(
            output_dir,
            aircraft_slug,
            target_slug,
            occupied_names,
        )
        occupied_names.add(output_path.name.casefold())
        jobs.append(
            TranspositionJob(
                input_path=input_path,
                output_path=output_path,
                aircraft_name=aircraft_name,
                aircraft_slug=aircraft_slug,
                target_airfield_slug=target_slug,
            )
        )

    return TranspositionPlan(output_directory=output_dir, jobs=tuple(jobs))


def _normalized_output_filename(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Each output filename must be text.")
    filename = value.strip()
    if not filename:
        raise ValueError("Output filenames cannot be empty.")
    if filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise ValueError(f'Output filename must not contain a folder: "{value}".')
    if any(character in filename for character in '<>:"|?*\0'):
        raise ValueError(f'Output filename contains an unsupported character: "{value}".')
    if not filename.lower().endswith(".kml"):
        filename = f"{filename}.kml"
    if len(filename) > 255:
        raise ValueError("Output filenames must be 255 characters or fewer.")
    return filename


def customize_transposition_plan(
    plan: TranspositionPlan,
    output_filenames: Sequence[str],
    approved_overwrites: Sequence[str | os.PathLike[str]] = (),
) -> TranspositionPlan:
    """Return a plan using exact, validated output filenames in job order."""
    if len(output_filenames) != len(plan.jobs):
        raise ValueError("Provide exactly one output filename for each input KML file.")

    filenames = tuple(_normalized_output_filename(value) for value in output_filenames)
    folded_names = [filename.casefold() for filename in filenames]
    if len(set(folded_names)) != len(folded_names):
        raise ValueError("Output filenames must be unique, ignoring letter case.")

    approved = {
        Path(path).resolve(strict=False) for path in approved_overwrites
    }
    output_paths = tuple(plan.output_directory / filename for filename in filenames)
    planned_paths = {path.resolve(strict=False) for path in output_paths}
    unknown_approvals = approved - planned_paths
    if unknown_approvals:
        raise ValueError("Overwrite approval includes a file outside this output plan.")

    jobs = tuple(
        TranspositionJob(
            input_path=job.input_path,
            output_path=output_path,
            aircraft_name=job.aircraft_name,
            aircraft_slug=job.aircraft_slug,
            target_airfield_slug=job.target_airfield_slug,
            overwrite_existing=output_path.resolve(strict=False) in approved,
            source_runway=job.source_runway,
        )
        for job, output_path in zip(plan.jobs, output_paths, strict=True)
    )
    return TranspositionPlan(output_directory=plan.output_directory, jobs=jobs)


def apply_source_runways(
    plan: TranspositionPlan,
    source_runways: Sequence[RunwayReference | None],
) -> TranspositionPlan:
    """Attach one reviewed source runway alignment to each planned input."""
    if len(source_runways) != len(plan.jobs):
        raise ValueError("Provide exactly one source runway review for each input KML file.")
    return TranspositionPlan(
        output_directory=plan.output_directory,
        jobs=tuple(
            TranspositionJob(
                input_path=job.input_path,
                output_path=job.output_path,
                aircraft_name=job.aircraft_name,
                aircraft_slug=job.aircraft_slug,
                target_airfield_slug=job.target_airfield_slug,
                overwrite_existing=job.overwrite_existing,
                source_runway=source_runway,
            )
            for job, source_runway in zip(plan.jobs, source_runways, strict=True)
        ),
    )


# def fetch_single_elevation(coordinate):
#     """
#     Fetch the ground elevation for a single (lat, lon) coordinate.
#     """
#     base_url = "https://api.open-elevation.com/api/v1/lookup"
#     response = requests.get(base_url, params={"locations": f"{coordinate[0]},{coordinate[1]}"})
#     if response.status_code == 200:
#         data = response.json()
#         if data.get('results'):
#             return data['results'][0]['elevation']
#         else:
#             raise Exception("No results in API response.")
#     else:
#         raise Exception(f"API Error: {response.status_code}, {response.text}")


def rotate_route(waypoints, target_lat, target_lon, target_heading):
    """Deprecated first-segment adapter retained for direct legacy callers."""
    warnings.warn(
        "rotate_route() infers alignment from the first segment and is deprecated; "
        "use transpose_wgs84_enu_points() with reviewed runway references.",
        DeprecationWarning,
        stacklevel=2,
    )
    if len(waypoints) < 2:
        raise ValueError("At least two waypoints are needed to calculate the initial heading.")
    start_lat, start_lon, _ = waypoints[0]
    next_lat, next_lon, _ = waypoints[1]
    distance, initial_heading = inverse_distance_bearing(
        start_lat, start_lon, next_lat, next_lon
    )
    if distance == 0.0:
        raise ValueError("The first two waypoints are identical and have no heading.")
    return list(
        transpose_wgs84_enu_points(
            waypoints,
            (start_lat, start_lon),
            (target_lat, target_lon),
            target_heading - initial_heading,
        )
    )


def _transposition_document(
    coordinates: Sequence[tuple[float, float, float]],
    name_of_aircraft: str,
    *,
    processing_warnings: Sequence[str] = (),
    line_colour: str = TRANSPOSITION_FALLBACK_LINE_COLOUR,
) -> KmlDocument:
    """Build the canonical document shared by preview and file export."""
    line_colour = line_colour.lower()
    track_style = KmlStyle(
        style_id="transposedTrackLine",
        line_colour=line_colour,
        line_width=6,
        poly_colour=f"33{line_colour[2:]}",
    )
    warning_description = None
    if processing_warnings:
        warning_description = "Processing warnings:\n" + "\n".join(
            f"- {warning}" for warning in processing_warnings
        )
    return KmlDocument(
        name=f"{name_of_aircraft} Adjusted Coordinates",
        styles=(track_style,),
        placemarks=(
            KmlPlacemark(
                name="Path",
                style_url="#transposedTrackLine",
                geometry=KmlLineString(
                    coordinates=tuple(
                        KmlCoordinate(longitude=lon, latitude=lat, altitude_m=alt)
                        for lat, lon, alt in coordinates
                    ),
                    altitude_mode="relativeToGround",
                    extrude_to_ground=True,
                    tessellate=False,
                ),
                description=warning_description,
            ),
        ),
    )


def write_kml(
    file_path,
    coordinates,
    name_of_aircraft,
    *,
    overwrite=False,
    processing_warnings: Sequence[str] = (),
    line_colour: str = TRANSPOSITION_FALLBACK_LINE_COLOUR,
    _document: KmlDocument | None = None,
):
    """Create a collision-safe, Google Earth-ready transposed KML output."""
    document = _document or _transposition_document(
        coordinates,
        name_of_aircraft,
        processing_warnings=processing_warnings,
        line_colour=line_colour,
    )
    export_kml(file_path, document, overwrite=overwrite)


def read_config(config_file):
    with open(config_file, "r", encoding="utf-8") as file:
        config = {}
        for line in file:
            line = line.strip()
            if " = " in line:
                key, value = line.split(" = ")
                value = value.strip().replace("\\", "")
                try:
                    config[key] = float(value)
                except ValueError:
                    LOGGER.warning("Invalid value for %s: %s", key, value)
                    continue
        return config


def _waypoints_for_transposition(
    track: KmlTrack,
    source_runway: RunwayReference,
) -> tuple[list[tuple[float, float, float]], tuple[str, ...]]:
    """Convert encoded KML heights to output relative-to-ground heights."""
    if track.altitude_mode == "absolute" and source_runway.elevation_m is None:
        raise ValueError(
            "Source ground-reference elevation is required for a KML using absolute altitude."
        )
    waypoints: list[tuple[float, float, float]] = []
    omitted_missing_altitudes = 0
    for point in track.points:
        if track.altitude_mode == "absolute":
            if point.altitude_m is None:
                omitted_missing_altitudes += 1
                continue
            altitude = point.altitude_m - source_runway.elevation_m
        elif track.altitude_mode == "relativeToGround":
            altitude = point.altitude_m if point.altitude_m is not None else 0.0
        elif track.altitude_mode == "clampToGround":
            altitude = 0.0
        else:
            raise ValueError(
                f'KML altitude mode "{track.altitude_mode}" cannot be converted '
                "safely to relative-to-ground output."
            )
        waypoints.append((point.latitude, point.longitude, altitude))
    if track.altitude_mode == "absolute" and len(waypoints) < 2:
        raise ValueError(
            f"Omitted {omitted_missing_altitudes} source coordinate(s) because "
            "absolute altitude was missing; fewer than two valid coordinates remain."
        )
    processing_warnings = (
        (
            f"Omitted {omitted_missing_altitudes} source coordinate(s) because "
            "absolute altitude was missing."
        ),
    ) if omitted_missing_altitudes else ()
    return waypoints, processing_warnings


def _failure_outcome(
    job: TranspositionJob,
    output_path: Path,
    code: TranspositionErrorCode,
    error: Exception,
) -> TranspositionFileOutcome:
    if isinstance(error, (ValueError, OSError)):
        message = str(error).strip() or "The file could not be transposed."
    else:
        message = "An unexpected error occurred while processing this file."
    LOGGER.warning(
        "Transposition failed for %s (%s): %s",
        job.input_path,
        code.value,
        message,
        exc_info=(type(error), error, error.__traceback__),
    )
    return TranspositionFileOutcome(
        input_path=job.input_path,
        planned_output_path=job.output_path,
        final_output_path=None,
        status=TranspositionFileStatus.FAILED,
        error=TranspositionError(
            code=code,
            message=message,
            input_path=job.input_path,
            intended_output_path=output_path,
            exception_type=error.__class__.__name__,
        ),
    )


def _preparation_failure(
    input_path: Path,
    code: TranspositionErrorCode,
    error: Exception,
) -> PreparedTranspositionFailure:
    if isinstance(error, (ValueError, OSError)):
        message = str(error).strip() or "The file could not be transposed."
    else:
        message = "An unexpected error occurred while processing this file."
    LOGGER.warning(
        "Transposition preparation failed for %s (%s): %s",
        input_path,
        code.value,
        message,
        exc_info=(type(error), error, error.__traceback__),
    )
    return PreparedTranspositionFailure(
        input_path=input_path,
        code=code,
        message=message,
        exception_type=error.__class__.__name__,
    )


def prepare_transposition(
    plan: TranspositionPlan | None = None,
    target_runway: RunwayReference | None = None,
    *,
    input_files: Sequence[str | os.PathLike[str]] | None = None,
    source_runways: Sequence[RunwayReference | None] | None = None,
) -> PreparedTranspositionBatch:
    """Prepare transposed KML documents in memory without writing files.

    Existing callers can provide a reviewed plan. Preview-first callers can
    provide ordered inputs and source runways before selecting an output folder.
    """
    if target_runway is None:
        raise TypeError("target_runway is required.")
    if plan is not None:
        if input_files is not None or source_runways is not None:
            raise TypeError(
                "Do not mix a TranspositionPlan with input_files or source_runways."
            )
        if not plan.jobs:
            raise ValueError("A transposition plan must contain at least one job.")
        sources = tuple(
            (job.input_path, job.aircraft_name, job.source_runway)
            for job in plan.jobs
        )
    else:
        if input_files is None or source_runways is None:
            raise TypeError(
                "Preview-first preparation requires input_files and source_runways."
            )
        if not input_files:
            raise ValueError("At least one input KML file is required.")
        if len(input_files) != len(source_runways):
            raise ValueError(
                "Provide exactly one source runway review for each input KML file."
            )
        sources = tuple(
            (Path(input_path), Path(input_path).stem, source_runway)
            for input_path, source_runway in zip(
                input_files, source_runways, strict=True
            )
        )

    anchor = KmlCoordinate(
        longitude=target_runway.longitude,
        latitude=target_runway.latitude,
        altitude_m=0.0,
    )
    items: list[PreparedTranspositionItem] = []
    label_counts = Counter(aircraft_name for _, aircraft_name, _ in sources)

    for index, (input_path, aircraft_name, reviewed_source) in enumerate(sources):
        LOGGER.info("Preparing transposition for %s", input_path)
        try:
            track = parse_kml_track(input_path)
        except Exception as error:
            items.append(
                _preparation_failure(
                    input_path, TranspositionErrorCode.INPUT_KML, error
                )
            )
            continue

        try:
            if reviewed_source is None:
                raise ValueError(
                    "Source runway alignment has not been reviewed for this input."
                )
            waypoints, processing_warnings = _waypoints_for_transposition(
                track,
                reviewed_source,
            )
            adjusted_waypoints = transpose_wgs84_enu_points(
                waypoints,
                (reviewed_source.latitude, reviewed_source.longitude),
                (target_runway.latitude, target_runway.longitude),
                target_runway.true_heading_deg
                - reviewed_source.true_heading_deg,
            )
            document = _transposition_document(
                adjusted_waypoints,
                aircraft_name,
                processing_warnings=processing_warnings,
                line_colour=(
                    track.source_line_colour
                    or TRANSPOSITION_FALLBACK_LINE_COLOUR
                ),
            )
            trace = PreparedTrace(
                trace_id=f"transposition-{index}",
                label=(
                    aircraft_name
                    if label_counts[aircraft_name] == 1
                    else f"{aircraft_name} — {input_path.parent}"
                ),
                anchor=anchor,
                base_document=document,
            )
        except Exception as error:
            items.append(
                _preparation_failure(
                    input_path,
                    TranspositionErrorCode.TRANSFORMATION,
                    error,
                )
            )
            continue

        items.append(
            PreparedTranspositionFile(
                input_path=input_path,
                aircraft_name=aircraft_name,
                trace=trace,
                warnings=processing_warnings,
            )
        )

    return PreparedTranspositionBatch(
        target_runway=target_runway,
        items=tuple(items),
    )


def _prepared_failure_outcome(
    job: TranspositionJob,
    failure: PreparedTranspositionFailure,
) -> TranspositionFileOutcome:
    return TranspositionFileOutcome(
        input_path=job.input_path,
        planned_output_path=job.output_path,
        final_output_path=None,
        status=TranspositionFileStatus.FAILED,
        error=TranspositionError(
            code=failure.code,
            message=failure.message,
            input_path=job.input_path,
            intended_output_path=job.output_path,
            exception_type=failure.exception_type,
        ),
    )


def _document_waypoints(
    document: KmlDocument,
) -> tuple[tuple[float, float, float], ...]:
    lines = tuple(
        placemark.geometry
        for placemark in document.placemarks
        if isinstance(placemark.geometry, KmlLineString)
    )
    if len(lines) != 1:
        raise ValueError(
            "A prepared transposition document must contain exactly one LineString."
        )
    return tuple(
        (coordinate.latitude, coordinate.longitude, coordinate.altitude_m)
        for coordinate in lines[0].coordinates
    )


def export_prepared_transposition(
    prepared_batch: PreparedTranspositionBatch,
    plan: TranspositionPlan,
) -> TranspositionBatchResult:
    """Write prepared documents with the supplied output plan."""
    _validate_transposition_plan(plan)
    if len(prepared_batch.items) != len(plan.jobs):
        raise ValueError(
            "The prepared batch and output plan must contain the same inputs."
        )
    for item, job in zip(prepared_batch.items, plan.jobs, strict=True):
        prepared_input = item.input_path.resolve(strict=False)
        planned_input = job.input_path.resolve(strict=False)
        if prepared_input != planned_input:
            raise ValueError(
                "Prepared inputs must match the output plan in the same order."
            )

    outcomes: list[TranspositionFileOutcome] = []
    for item, job in zip(prepared_batch.items, plan.jobs, strict=True):
        output_path = job.output_path
        if isinstance(item, PreparedTranspositionFailure):
            outcomes.append(_prepared_failure_outcome(job, item))
            continue

        LOGGER.info("Exporting prepared transposition for %s", job.input_path)
        try:
            write_kml(
                output_path,
                _document_waypoints(item.document),
                job.aircraft_name,
                overwrite=job.overwrite_existing,
                processing_warnings=item.warnings,
                _document=item.document,
            )
        except FileExistsError as error:
            outcomes.append(
                _failure_outcome(
                    job,
                    output_path,
                    TranspositionErrorCode.OUTPUT_COLLISION,
                    error,
                )
            )
            continue
        except Exception as error:
            outcomes.append(
                _failure_outcome(
                    job, output_path, TranspositionErrorCode.FILESYSTEM_WRITE, error
                )
            )
            continue

        outcomes.append(
            TranspositionFileOutcome(
                input_path=job.input_path,
                planned_output_path=job.output_path,
                final_output_path=output_path,
                status=TranspositionFileStatus.SUCCEEDED,
                warnings=item.warnings,
            )
        )
        LOGGER.info("Transposition saved to %s", output_path)

    return TranspositionBatchResult(outcomes=tuple(outcomes))


def _validate_transposition_plan(plan: TranspositionPlan) -> None:
    if not plan.jobs:
        raise ValueError("A transposition plan must contain at least one job.")
    if not plan.output_directory.is_dir():
        raise ValueError(
            f'Output directory does not exist: "{plan.output_directory}".'
        )
    if any(job.output_path.parent != plan.output_directory for job in plan.jobs):
        raise ValueError("Every planned output must be inside the output directory.")
    names = [job.output_path.name.casefold() for job in plan.jobs]
    if len(names) != len(set(names)):
        raise ValueError("Every planned output filename must be unique.")


def _legacy_plan(
    input_files: Sequence[str | os.PathLike[str]], output_file: str | os.PathLike[str]
) -> TranspositionPlan:
    if len(input_files) != 1:
        raise ValueError(
            "The deprecated output_file API accepts exactly one input KML file; "
            "use create_transposition_plan() for batch transposition."
        )
    input_path = Path(input_files[0])
    output_path = Path(output_file)
    if input_path.suffix.lower() != ".kml":
        raise ValueError(f"{input_path.name}: expected a KML file.")
    if not output_path.parent.is_dir():
        raise ValueError(f'Output directory does not exist: "{output_path.parent}".')
    return TranspositionPlan(
        output_directory=output_path.parent,
        jobs=(
            TranspositionJob(
                input_path=input_path,
                output_path=output_path,
                aircraft_name=input_path.stem,
                aircraft_slug=_slug_component(input_path.stem, "aircraft"),
                target_airfield_slug="legacy-output",
            ),
        ),
    )


def run_transposition(
    plan: TranspositionPlan | Sequence[str | os.PathLike[str]] | None = None,
    *legacy_args: object,
    target_lat: float | None = None,
    target_lon: float | None = None,
    target_heading: float | None = None,
    ground_reference_elevation: float | None = None,
    target_runway: RunwayReference | None = None,
    source_runway: RunwayReference | None = None,
    input_files: Sequence[str | os.PathLike[str]] | None = None,
    output_file: str | os.PathLike[str] | None = None,
) -> TranspositionBatchResult:
    """Run reviewed runway-to-runway jobs and return one outcome per input.

    The legacy ``input_files``/``output_file`` invocation is supported for one
    input only and is deprecated; new callers must create a plan first.
    """
    if isinstance(plan, TranspositionPlan):
        if input_files is not None or output_file is not None:
            raise TypeError("Do not mix a TranspositionPlan with legacy arguments.")
        positional = list(legacy_args)
        for name in ("target_lat", "target_lon", "target_heading"):
            if locals()[name] is None and positional:
                value = positional.pop(0)
                if name == "target_lat":
                    target_lat = value  # type: ignore[assignment]
                elif name == "target_lon":
                    target_lon = value  # type: ignore[assignment]
                else:
                    target_heading = value  # type: ignore[assignment]
        if positional:
            ground_reference_elevation = positional.pop(0)  # type: ignore[assignment]
        if positional:
            raise TypeError("Too many positional arguments for transposition.")
        active_plan = plan
    else:
        if input_files is not None and plan is not None:
            raise TypeError("Provide legacy input files once.")
        legacy_inputs = input_files if input_files is not None else plan
        positional = list(legacy_args)
        if output_file is None and positional:
            output_file = positional.pop(0)  # type: ignore[assignment]
        for name in ("target_lat", "target_lon", "target_heading"):
            if locals()[name] is None and positional:
                value = positional.pop(0)
                if name == "target_lat":
                    target_lat = value  # type: ignore[assignment]
                elif name == "target_lon":
                    target_lon = value  # type: ignore[assignment]
                else:
                    target_heading = value  # type: ignore[assignment]
        if positional:
            ground_reference_elevation = positional.pop(0)  # type: ignore[assignment]
        if positional or legacy_inputs is None or output_file is None:
            raise TypeError("Legacy transposition requires input_files and output_file.")
        warnings.warn(
            "input_files/output_file transposition is deprecated; create a "
            "TranspositionPlan and pass it as plan= instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        active_plan = _legacy_plan(legacy_inputs, output_file)

    if source_runway is not None:
        if len(active_plan.jobs) != 1:
            raise ValueError("source_runway can only be used with one input job.")
        active_plan = apply_source_runways(active_plan, (source_runway,))
    if ground_reference_elevation is not None:
        if len(active_plan.jobs) != 1:
            raise ValueError(
                "A shared ground_reference_elevation is not valid for a batch; "
                "set elevation_m on each source runway."
            )
        reviewed_source = active_plan.jobs[0].source_runway
        if reviewed_source is not None:
            if reviewed_source.elevation_m is not None:
                raise TypeError(
                    "Do not mix ground_reference_elevation with source runway elevation."
                )
            warnings.warn(
                "ground_reference_elevation is deprecated; set elevation_m on the "
                "source RunwayReference.",
                DeprecationWarning,
                stacklevel=2,
            )
            active_plan = apply_source_runways(
                active_plan,
                (
                    RunwayReference(
                        reviewed_source.latitude,
                        reviewed_source.longitude,
                        reviewed_source.true_heading_deg,
                        float(ground_reference_elevation),
                    ),
                ),
            )
    if target_runway is not None:
        if any(value is not None for value in (target_lat, target_lon, target_heading)):
            raise TypeError("Do not mix target_runway with legacy target coordinates.")
        resolved_target = target_runway
    else:
        if target_lat is None or target_lon is None or target_heading is None:
            raise TypeError("target_runway is required.")
        warnings.warn(
            "target_lat/target_lon/target_heading are deprecated; pass a "
            "RunwayReference as target_runway.",
            DeprecationWarning,
            stacklevel=2,
        )
        resolved_target = RunwayReference(
            float(target_lat),
            float(target_lon),
            float(target_heading),
        )
    _validate_transposition_plan(active_plan)
    prepared_batch = prepare_transposition(
        plan=active_plan,
        target_runway=resolved_target,
    )
    return export_prepared_transposition(prepared_batch, active_plan)


if __name__ == "__main__":
    try:
        if getattr(sys, "frozen", False):
            app_path = Path(sys.executable).parent
        else:
            app_path = Path(__file__).resolve().parent

        input_dir = app_path / "Input_KML_Files"
        output_dir = app_path / "Output_KML_Files"
        output_dir.mkdir(parents=True, exist_ok=True)

        config = read_config(app_path / "config.txt")
        input_files = sorted(
            path for path in input_dir.iterdir() if path.suffix.lower() == ".kml"
        )
        plan = create_transposition_plan(input_files, output_dir, "Farnborough")
        result = run_transposition(
            plan,
            target_lat=config["target_lat"],
            target_lon=config["target_lon"],
            target_heading=config["target_heading"],
        )
        for output in result.successful:
            print(f"Transposed coordinates saved to {output.output_path}")
        if result.failure_count:
            print(f"{result.failure_count} file(s) failed:")
            for outcome in result.failed_outcomes:
                print(f"{outcome.input_path.name}: {outcome.error.message}")
            raise SystemExit(1)
        print("All files processed successfully.")
    except Exception as error:
        print(f"Error: {error}")
        raise SystemExit(1)
