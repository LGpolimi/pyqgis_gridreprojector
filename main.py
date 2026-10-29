from __future__ import annotations

import gc
import os
import shutil
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

APP_NAME = "Grid Reprojector"
APP_VERSION = "1.0.0"


def _handle_early_cli() -> None:
    if "--version" in sys.argv:
        print(f"{APP_NAME} {APP_VERSION}")
        raise SystemExit(0)
    if "--help" in sys.argv or "-h" in sys.argv:
        print(
            "Grid Reprojector\n"
            "Usage: python main.py [--qgis-prefix <QGIS_PREFIX>]\n\n"
            "Options:\n"
            "  --qgis-prefix PATH  Explicit QGIS application prefix or installation root.\n"
            "  --version           Print the application version.\n"
            "  -h, --help          Print this help message.\n\n"
            "Normally no QGIS path option is required when the application is launched "
            "from a QGIS/OSGeo4W Python environment."
        )
        raise SystemExit(0)


_handle_early_cli()

from qgis_runtime import configure_from_argv

# Configure a portable PyQGIS runtime before importing modules that depend on QGIS.
try:
    QGIS_PREFIX = configure_from_argv()
except RuntimeError as exc:
    print(f"{APP_NAME} startup error: {exc}", file=sys.stderr)
    raise SystemExit(2)

import base_reprojector as base
import timeseries_reprojector as temporal

from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsApplication, QgsProject, QgsVectorFileWriter, QgsVectorLayer


FORMAT_GPKG = "GeoPackage (.gpkg)"
FORMAT_CSV = "CSV (.csv)"
FORMAT_SHP = "Shapefile (.shp)"
OUTPUT_FORMATS = (FORMAT_GPKG, FORMAT_CSV, FORMAT_SHP)


def output_extension(output_format: str) -> str:
    if output_format == FORMAT_CSV:
        return ".csv"
    if output_format == FORMAT_SHP:
        return ".shp"
    return ".gpkg"


def output_filter(output_format: str) -> str:
    if output_format == FORMAT_CSV:
        return "CSV files (*.csv)"
    if output_format == FORMAT_SHP:
        return "ESRI Shapefile (*.shp)"
    return "GeoPackage (*.gpkg)"


def output_driver(output_format: str) -> str:
    if output_format == FORMAT_CSV:
        return "CSV"
    if output_format == FORMAT_SHP:
        return "ESRI Shapefile"
    return "GPKG"


def ensure_suffix(path: Path, output_format: str) -> Path:
    extension = output_extension(output_format)
    if path.suffix.lower() != extension:
        path = path.with_suffix(extension)
    return path


def source_file_path(source: str) -> Path:
    return Path(source.split("|", 1)[0])


def path_identity(path: Path) -> str:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path.expanduser().absolute()
    return os.path.normcase(str(resolved))


def configured_source_paths(page: base.GridTransferWindow) -> List[Path]:
    paths: List[Path] = []
    if page.input_selector.layer is not None:
        paths.append(source_file_path(page.input_selector.layer.source()))
    if page.output_selector.layer is not None:
        paths.append(source_file_path(page.output_selector.layer.source()))
    if page.existing_intersection_layer is not None:
        paths.append(source_file_path(page.existing_intersection_layer.source()))
    alt = page.alternative_weight_config
    if alt is not None and alt.intersection_path:
        paths.append(source_file_path(alt.intersection_path))
    return paths


def assert_outputs_do_not_replace_sources(
    outputs: Sequence[Path],
    sources: Sequence[Path],
) -> None:
    source_ids = {path_identity(path): path for path in sources}
    collisions = []
    for output in outputs:
        source = source_ids.get(path_identity(output))
        if source is not None:
            collisions.append((output, source))
    if collisions:
        details = "\n".join(
            f"Output '{output}' conflicts with source '{source}'."
            for output, source in collisions
        )
        raise ValueError(
            "An output file must not replace an input, destination-grid, intersection, "
            "or weight source file. Choose a different output name/folder.\n" + details
        )


def source_uri_for_candidate(sample_source: str, candidate: Path) -> str:
    """Reuse a GeoPackage layer selector suffix, when one exists on the example source."""
    if "|" not in sample_source:
        return str(candidate)
    suffix = sample_source.split("|", 1)[1]
    if candidate.suffix.lower() == ".gpkg":
        return f"{candidate}|{suffix}"
    return str(candidate)


def remove_shapefile_family(path: Path) -> None:
    """Remove a pre-existing Shapefile and its common sidecars before overwrite."""
    stem = path.with_suffix("")
    for suffix in (
        ".shp",
        ".shx",
        ".dbf",
        ".prj",
        ".cpg",
        ".qpj",
        ".sbn",
        ".sbx",
        ".shp.xml",
    ):
        candidate = Path(str(stem) + suffix)
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def remove_output_file(path: Path, output_format: str) -> None:
    if output_format == FORMAT_SHP:
        remove_shapefile_family(path)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def release_qgis_layer(layer: Optional[QgsVectorLayer]) -> None:
    if layer is None:
        return
    try:
        if not sip.isdeleted(layer):
            sip.delete(layer)
    except Exception:
        pass
    gc.collect()
    QgsApplication.processEvents()


def export_vector_layer(
    source: str,
    destination: Path,
    output_format: str,
    layer_name: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    remove_output_file(destination, output_format)

    layer = QgsVectorLayer(source, layer_name, "ogr")
    if not layer.isValid():
        release_qgis_layer(layer)
        raise RuntimeError(f"Could not open generated layer for export: {source}")

    try:
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = output_driver(output_format)
        options.fileEncoding = "UTF-8"
        options.layerName = layer_name
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
        if output_format == FORMAT_CSV:
            # Geometry is intentionally omitted from CSV output. The output remains
            # an ordinary attribute table keyed by the destination-grid UID.
            options.layerOptions = ["GEOMETRY=NONE"]

        error, error_message, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer,
            str(destination),
            QgsProject.instance().transformContext(),
            options,
        )
        if error != QgsVectorFileWriter.NoError:
            raise RuntimeError(
                f"Could not export '{layer_name}' to {destination}: {error_message}"
            )
    finally:
        release_qgis_layer(layer)


def temporary_gpkg_for(destination: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return destination.with_name(
        f"{destination.stem}_gridreprojector_work_{os.getpid()}_{stamp}.gpkg"
    )


def safe_remove_working_files(path: Path) -> None:
    """Best-effort cleanup of a temporary GeoPackage and its warning log."""
    warning_log = path.with_name(path.stem + "_warnings.log")
    for candidate in (warning_log, path):
        for _ in range(12):
            try:
                candidate.unlink()
                break
            except FileNotFoundError:
                break
            except PermissionError:
                gc.collect()
                QgsApplication.processEvents()
                time.sleep(0.15)
            except OSError:
                break
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            Path(str(path) + suffix).unlink()
        except OSError:
            pass


def move_and_rewrite_warning_log(
    working_gpkg: Path,
    final_output: Path,
    output_format: str,
) -> Optional[Path]:
    source_log = working_gpkg.with_name(working_gpkg.stem + "_warnings.log")
    if not source_log.exists():
        return None
    target_log = final_output.with_name(final_output.stem + "_warnings.log")
    try:
        text = source_log.read_text(encoding="utf-8")
        text = text.replace(
            f"Output GeoPackage: {working_gpkg}",
            f"Output: {final_output}\nOutput format: {output_format}",
        )
        target_log.write_text(text, encoding="utf-8")
        try:
            source_log.unlink()
        except OSError:
            pass
        return target_log
    except Exception:
        try:
            shutil.copy2(source_log, target_log)
            return target_log
        except Exception:
            return source_log


def materialize_non_gpkg_output(
    working_gpkg: Path,
    final_output: Path,
    output_format: str,
    template_only: bool,
) -> Dict[str, Optional[Path]]:
    """Export the base engine's GeoPackage results to CSV or Shapefile."""
    if output_format == FORMAT_GPKG:
        return {"primary": final_output, "intersections": None}

    if template_only:
        export_vector_layer(
            f"{working_gpkg}|layername=grid_intersections",
            final_output,
            output_format,
            "grid_intersections",
        )
        return {"primary": final_output, "intersections": None}

    export_vector_layer(
        f"{working_gpkg}|layername=reprojected_output",
        final_output,
        output_format,
        "reprojected_output",
    )
    intersection_output = final_output.with_name(
        final_output.stem + "_grid_intersections" + final_output.suffix
    )
    export_vector_layer(
        f"{working_gpkg}|layername=grid_intersections",
        intersection_output,
        output_format,
        "grid_intersections",
    )
    return {"primary": final_output, "intersections": intersection_output}


class BaseOutputPage(base.GridTransferWindow):
    """Original base GUI plus selectable final output format."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.Widget)
        self.setWindowTitle("Base version")
        self._install_output_format_controls()

    def _install_output_format_controls(self) -> None:
        output_group = next(
            (g for g in self.findChildren(QGroupBox) if g.title() == "Output"),
            None,
        )
        if output_group is None or not isinstance(output_group.layout(), QFormLayout):
            raise RuntimeError("Could not locate the base Output group.")
        form: QFormLayout = output_group.layout()

        self.output_format_combo = QComboBox()
        self.output_format_combo.addItems(OUTPUT_FORMATS)
        self.output_format_combo.setCurrentText(FORMAT_GPKG)
        form.insertRow(0, "Output format", self.output_format_combo)

        self._destination_label: Optional[QLabel] = None
        for label in output_group.findChildren(QLabel):
            if label.text() == "Destination GeoPackage":
                self._destination_label = label
                label.setText("Destination file")
                break

        self.output_format_combo.currentIndexChanged.connect(self._output_format_changed)
        self._output_format_changed()

    def _output_format_changed(self, *_args) -> None:
        current = self.destination_edit.text().strip()
        if current:
            self.destination_edit.setText(
                str(ensure_suffix(Path(current), self.output_format_combo.currentText()))
            )

        template_only = bool(
            self.weighting_combo.currentText() == base.WEIGHT_ALTERNATIVE
            and self.alternative_weight_config is not None
            and self.alternative_weight_config.template_only
        )
        if template_only:
            if self.output_format_combo.currentText() == FORMAT_SHP:
                text = "grid_intersections weight-preparation template Shapefile only"
            elif self.output_format_combo.currentText() == FORMAT_CSV:
                text = "grid_intersections weight-preparation template CSV only"
            else:
                text = "grid_intersections only, weight preparation template"
            self.layers_created_label.setText(text)
            return

        if self.output_format_combo.currentText() == FORMAT_SHP:
            self.layers_created_label.setText(
                "reprojected output Shapefile + companion grid_intersections Shapefile"
            )
        elif self.output_format_combo.currentText() == FORMAT_CSV:
            self.layers_created_label.setText(
                "reprojected output CSV + companion grid_intersections CSV"
            )
        else:
            self.layers_created_label.setText("grid_intersections and reprojected_output")

    def _weighting_strategy_changed(self, index: int) -> None:
        base.GridTransferWindow._weighting_strategy_changed(self, index)
        if hasattr(self, "output_format_combo"):
            self._output_format_changed()

    def _configure_alternative_weighting(self) -> bool:
        result = base.GridTransferWindow._configure_alternative_weighting(self)
        if hasattr(self, "output_format_combo"):
            self._output_format_changed()
        return result

    def _browse_destination(self) -> None:
        output_format = self.output_format_combo.currentText()
        default_name = self.destination_edit.text().strip() or (
            "grid_transfer" + output_extension(output_format)
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Select destination file",
            default_name,
            output_filter(output_format),
        )
        if path:
            self.destination_edit.setText(
                str(ensure_suffix(Path(path), output_format))
            )

    def _final_destination(self) -> Path:
        value = self.destination_edit.text().strip()
        if not value:
            raise ValueError("Select a destination file.")
        output_format = self.output_format_combo.currentText()
        destination = ensure_suffix(Path(value), output_format)
        self.destination_edit.setText(str(destination))
        return destination

    def _configuration_for_working_gpkg(self, working_gpkg: Path):
        original = self.destination_edit.text()
        self.destination_edit.setText(str(working_gpkg))
        try:
            return base.GridTransferWindow._build_configuration(self)
        finally:
            self.destination_edit.setText(original)

    def _run(self) -> None:
        try:
            final_destination = self._final_destination()
            output_format = self.output_format_combo.currentText()
            final_destination.parent.mkdir(parents=True, exist_ok=True)
            working_gpkg = (
                final_destination
                if output_format == FORMAT_GPKG
                else temporary_gpkg_for(final_destination)
            )
            configuration = self._configuration_for_working_gpkg(working_gpkg)
        except Exception as exc:
            QMessageBox.warning(self, "Configuration error", str(exc))
            return

        output_targets = [final_destination]
        if output_format != FORMAT_GPKG and not (
            configuration.weighting_strategy == base.WEIGHT_ALTERNATIVE
            and configuration.alternative_weight is not None
            and configuration.alternative_weight.template_only
        ):
            output_targets.append(
                final_destination.with_name(
                    final_destination.stem
                    + "_grid_intersections"
                    + final_destination.suffix
                )
            )
        try:
            assert_outputs_do_not_replace_sources(
                output_targets, configured_source_paths(self)
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Unsafe output path", str(exc))
            return

        existing_targets = [p for p in output_targets if p.exists()]
        if existing_targets:
            answer = QMessageBox.question(
                self,
                "Overwrite output",
                "The following output already exists and will be replaced:\n"
                + "\n".join(str(p) for p in existing_targets)
                + "\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        self.run_button.setEnabled(False)
        self.log.clear()
        self.progress.setValue(0)
        self._log(
            f"Starting transfer with weighting strategy: {configuration.weighting_strategy}."
        )
        self._log(f"Final output format: {output_format}.")
        self._log(
            "Variables to reproject: "
            + ", ".join(
                f"{field_name} ({category})"
                for field_name, category in configuration.variables
            )
        )
        self._log(
            "Weight used: "
            + base.describe_weighting_configuration(
                configuration.weighting_strategy,
                configuration.alternative_weight,
            )
        )

        try:
            processor = base.GridTransferProcessor(
                configuration, self._log, self._set_progress
            )
            summary = processor.run()

            exported = {"primary": final_destination, "intersections": None}
            if output_format != FORMAT_GPKG:
                exported = materialize_non_gpkg_output(
                    working_gpkg,
                    final_destination,
                    output_format,
                    bool(summary["template_only"]),
                )
                warning_log = move_and_rewrite_warning_log(
                    working_gpkg, final_destination, output_format
                )
                if warning_log is not None:
                    summary["warning_log"] = str(warning_log)
                safe_remove_working_files(working_gpkg)

            self.progress.setValue(100)
            self._log("Processing completed successfully.")
            self._log(f"Primary output: {exported['primary']}")
            if exported.get("intersections"):
                self._log(f"Intersection output: {exported['intersections']}")
            self._log(f"Warning log: {summary['warning_log']}")
            if output_format == FORMAT_SHP:
                self._log(
                    "Warning: Shapefile DBF field names are limited and may be shortened by the driver."
                )

            message = (
                "Grid transfer completed.\n\n"
                f"Output: {exported['primary']}\n"
                f"Warning log: {summary['warning_log']}"
            )
            if exported.get("intersections"):
                message += f"\nIntersections: {exported['intersections']}"
            QMessageBox.information(self, "Completed", message)
        except Exception as exc:
            self._log(base.traceback.format_exc())
            QMessageBox.critical(self, "Processing error", str(exc))
        finally:
            self._update_run_state()


class MultifileOutputPage(BaseOutputPage):
    """Repeat the base transfer for matching input files in the example file's folder."""

    def __init__(self) -> None:
        self._batch_key_manually_edited = False
        super().__init__()
        self.setWindowTitle("Multifile version")
        self._install_batch_controls()
        self.input_selector.layer_changed.connect(self._update_default_batch_key)

    def _install_batch_controls(self) -> None:
        group = QGroupBox("Multifile iteration")
        form = QFormLayout(group)

        self.batch_key_edit = QLineEdit()
        self.batch_key_edit.setPlaceholderText(
            "Text that must occur in the input filename, default is the example extension"
        )
        self.batch_key_edit.textEdited.connect(self._mark_batch_key_manual)

        self.batch_output_directory_edit = QLineEdit()
        self.batch_output_directory_button = QPushButton("Browse")
        output_row = QHBoxLayout()
        output_row.addWidget(self.batch_output_directory_edit, 1)
        output_row.addWidget(self.batch_output_directory_button)

        self.batch_filename_prefix_edit = QLineEdit("tr_")
        self.batch_match_label = QLabel("Load an example input file to define the iteration folder.")
        self.batch_match_label.setWordWrap(True)

        form.addRow("Filename key string", self.batch_key_edit)
        form.addRow("Output folder", output_row)
        form.addRow("Output filename prefix", self.batch_filename_prefix_edit)
        form.addRow("Matching files", self.batch_match_label)

        central_layout = self.centralWidget().layout()
        insert_at = central_layout.indexOf(self.progress)
        central_layout.insertWidget(insert_at if insert_at >= 0 else central_layout.count(), group)

        # The single-file destination controls are not used in multifile mode.
        self.destination_edit.hide()
        self.destination_button.hide()
        if self._destination_label is not None:
            self._destination_label.hide()

        self.batch_output_directory_button.clicked.connect(self._browse_batch_output_directory)
        self.batch_key_edit.textChanged.connect(self._refresh_batch_match_label)
        self.batch_output_directory_edit.textChanged.connect(self._update_run_state)
        self.batch_filename_prefix_edit.textChanged.connect(self._update_run_state)
        self.run_button.setText("Run multifile grid transfer")

    def _mark_batch_key_manual(self, _text: str) -> None:
        self._batch_key_manually_edited = True

    def _update_default_batch_key(self) -> None:
        if self.input_selector.layer is None:
            return
        path = source_file_path(self.input_selector.layer.source())
        if not self._batch_key_manually_edited or not self.batch_key_edit.text().strip():
            self.batch_key_edit.blockSignals(True)
            self.batch_key_edit.setText(path.suffix)
            self.batch_key_edit.blockSignals(False)
        if not self.batch_output_directory_edit.text().strip():
            self.batch_output_directory_edit.setText(str(path.parent))
        self._refresh_batch_match_label()

    def _browse_batch_output_directory(self) -> None:
        start = self.batch_output_directory_edit.text().strip()
        if not start and self.input_selector.layer is not None:
            start = str(source_file_path(self.input_selector.layer.source()).parent)
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select multifile output folder",
            start,
        )
        if folder:
            self.batch_output_directory_edit.setText(folder)

    def _excluded_iteration_paths(self) -> set:
        excluded = set()
        if self.output_selector.layer is not None:
            excluded.add(source_file_path(self.output_selector.layer.source()).resolve())
        if self.existing_intersection_layer is not None:
            excluded.add(source_file_path(self.existing_intersection_layer.source()).resolve())
        alt = self.alternative_weight_config
        if alt is not None and alt.intersection_path:
            excluded.add(source_file_path(alt.intersection_path).resolve())
        return excluded

    def _matching_input_files(self) -> List[Path]:
        if self.input_selector.layer is None:
            return []
        example = source_file_path(self.input_selector.layer.source())
        key = self.batch_key_edit.text().strip().casefold()
        if not key:
            return []
        excluded = self._excluded_iteration_paths()
        candidates: List[Path] = []
        for path in sorted(example.parent.iterdir(), key=lambda p: p.name.casefold()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".gpkg", ".shp"}:
                continue
            if key not in path.name.casefold():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in excluded:
                continue
            candidates.append(path)
        return candidates

    def _refresh_batch_match_label(self, *_args) -> None:
        if self.input_selector.layer is None:
            self.batch_match_label.setText(
                "Load an example input file to define the iteration folder."
            )
            return
        candidates = self._matching_input_files()
        example = source_file_path(self.input_selector.layer.source())
        self.batch_match_label.setText(
            f"Folder: {example.parent} | matching supported input files: {len(candidates):,}"
        )

    def _update_run_state(self) -> None:
        # During the parent constructor the batch widgets do not exist yet.
        base.GridTransferWindow._update_run_state(self)
        if not hasattr(self, "batch_key_edit"):
            return
        ready = self.run_button.isEnabled()
        ready = (
            ready
            and bool(self.batch_key_edit.text().strip())
            and bool(self.batch_output_directory_edit.text().strip())
        )
        self.run_button.setEnabled(bool(ready))

    def _batch_output_path(self, input_file: Path) -> Path:
        folder = Path(self.batch_output_directory_edit.text().strip())
        prefix = self.batch_filename_prefix_edit.text()
        output_format = self.output_format_combo.currentText()
        return folder / (prefix + input_file.stem + output_extension(output_format))

    def _run(self) -> None:
        candidates = self._matching_input_files()
        if not candidates:
            QMessageBox.warning(
                self,
                "No matching files",
                "No supported .gpkg or .shp input files in the example file's folder contain "
                "the configured key string.",
            )
            return

        output_folder = Path(self.batch_output_directory_edit.text().strip())
        try:
            output_folder.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            QMessageBox.warning(self, "Invalid output folder", str(exc))
            return

        output_format = self.output_format_combo.currentText()
        sample_source = self.input_selector.layer.source() if self.input_selector.layer else ""

        # Validate the full base configuration once using a harmless placeholder path.
        try:
            placeholder = output_folder / "_gridreprojector_configuration_check.gpkg"
            template_config = self._configuration_for_working_gpkg(placeholder)
        except Exception as exc:
            QMessageBox.warning(self, "Configuration error", str(exc))
            return

        output_paths = [self._batch_output_path(path) for path in candidates]
        protected_sources = [*candidates, *configured_source_paths(self)]
        companion_outputs: List[Path] = []
        template_only = bool(
            template_config.weighting_strategy == base.WEIGHT_ALTERNATIVE
            and template_config.alternative_weight is not None
            and template_config.alternative_weight.template_only
        )
        if output_format != FORMAT_GPKG and not template_only:
            companion_outputs = [
                path.with_name(path.stem + "_grid_intersections" + path.suffix)
                for path in output_paths
            ]
        try:
            assert_outputs_do_not_replace_sources(
                [*output_paths, *companion_outputs], protected_sources
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Unsafe multifile output paths", str(exc))
            return

        existing = [path for path in [*output_paths, *companion_outputs] if path.exists()]
        if existing:
            answer = QMessageBox.question(
                self,
                "Overwrite multifile outputs",
                f"{len(existing):,} primary output file(s) already exist and will be replaced.\n\n"
                "Continue with the complete batch?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        self.run_button.setEnabled(False)
        self.log.clear()
        self.progress.setValue(0)
        self._log(
            f"Starting multifile transfer for {len(candidates):,} file(s). "
            f"Filename key: '{self.batch_key_edit.text().strip()}'."
        )
        self._log(f"Final output format: {output_format}.")

        successes: List[Tuple[Path, Path]] = []
        failures: List[Tuple[Path, str]] = []
        total = len(candidates)

        for index, input_file in enumerate(candidates, start=1):
            final_destination = self._batch_output_path(input_file)
            working_gpkg = (
                final_destination
                if output_format == FORMAT_GPKG
                else temporary_gpkg_for(final_destination)
            )
            input_source = source_uri_for_candidate(sample_source, input_file)
            config = replace(
                template_config,
                input_path=input_source,
                destination_gpkg=str(working_gpkg),
            )

            self._log(f"[{index}/{total}] Processing {input_file.name}")

            def file_log(message: str, name=input_file.name) -> None:
                self._log(f"[{name}] {message}")

            def file_progress(value: int, batch_index=index) -> None:
                value = max(0, min(100, int(value)))
                overall = int(((batch_index - 1) + value / 100.0) * 100 / total)
                self.progress.setValue(overall)
                QgsApplication.processEvents()

            try:
                processor = base.GridTransferProcessor(config, file_log, file_progress)
                summary = processor.run()
                if output_format != FORMAT_GPKG:
                    materialize_non_gpkg_output(
                        working_gpkg,
                        final_destination,
                        output_format,
                        bool(summary["template_only"]),
                    )
                    move_and_rewrite_warning_log(
                        working_gpkg, final_destination, output_format
                    )
                    safe_remove_working_files(working_gpkg)
                successes.append((input_file, final_destination))
                self._log(f"[{input_file.name}] Completed -> {final_destination.name}")
            except Exception as exc:
                failures.append((input_file, str(exc)))
                self._log(f"[{input_file.name}] ERROR: {exc}")

        self.progress.setValue(100)
        report_path = output_folder / (
            (self.batch_filename_prefix_edit.text() or "") + "multifile_report.log"
        )
        report_lines = [
            "Grid Reprojector multifile report",
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            f"Example input: {source_file_path(sample_source)}",
            f"Filename key: {self.batch_key_edit.text().strip()}",
            f"Output folder: {output_folder}",
            f"Output filename prefix: {self.batch_filename_prefix_edit.text()}",
            f"Output format: {output_format}",
            f"Files discovered: {len(candidates)}",
            f"Successful: {len(successes)}",
            f"Failed: {len(failures)}",
            "",
            "Successful files",
            "----------------",
            *(f"{src.name} -> {dst.name}" for src, dst in successes),
        ]
        if failures:
            report_lines.extend(
                [
                    "",
                    "Failed files",
                    "------------",
                    *(f"{src.name}: {message}" for src, message in failures),
                ]
            )
        report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

        self._log(
            f"Multifile processing finished: {len(successes):,} successful, "
            f"{len(failures):,} failed. Report: {report_path}"
        )
        if output_format == FORMAT_SHP:
            self._log(
                "Warning: Shapefile DBF field names are limited and may be shortened by the driver."
            )

        QMessageBox.information(
            self,
            "Multifile processing completed",
            f"Files processed: {len(candidates)}\n"
            f"Successful: {len(successes)}\n"
            f"Failed: {len(failures)}\n\n"
            f"Batch report: {report_path}",
        )
        self._update_run_state()


class GridReprojectorMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1220, 980)

        tabs = QTabWidget()
        self.setCentralWidget(tabs)

        self.base_page = BaseOutputPage()
        self.multifile_page = MultifileOutputPage()
        self.timeseries_page = temporal.TemporalGridReprojectorWindow()
        self.timeseries_page.setWindowFlags(Qt.Widget)

        for page in (self.base_page, self.multifile_page, self.timeseries_page):
            # All legacy close buttons should close the combined application rather
            # than hiding an individual embedded page.
            try:
                page.close_button.clicked.disconnect()
            except Exception:
                pass
            page.close_button.clicked.connect(self.close)

        tabs.addTab(self.base_page, "Base version")
        tabs.addTab(self.multifile_page, "Multifile version")
        tabs.addTab(self.timeseries_page, "Timeseries version")


def main() -> int:
    QgsApplication.setPrefixPath(QGIS_PREFIX, True)
    qgis_argv = [os.fsencode(argument) for argument in sys.argv]
    app = QgsApplication(qgis_argv, True)
    app.initQgis()
    try:
        window = GridReprojectorMainWindow()
        window.show()
        if hasattr(app, "exec_"):
            return app.exec_()
        return app.exec()
    finally:
        app.exitQgis()


if __name__ == "__main__":
    sys.exit(main())
