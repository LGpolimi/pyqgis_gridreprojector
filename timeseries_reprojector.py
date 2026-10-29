from __future__ import annotations

"""
Temporal Grid Reprojector
=========================

Standalone PyQGIS application for transferring a time x spatial-cell CSV matrix
from an input polygon grid to an output polygon grid.

Expected CSV structure
----------------------
One row per time observation and one column per input-grid polygon, e.g.:

    DATE_STR,9,10,40,...
    200101,0.45,0.44,0.51,...
    200102,1.01,0.94,1.10,...

The spatial column names are matched against the selected UID field of the input
grid. The output CSV preserves the selected time/key column and replaces input
cell columns with output-grid UID columns.

Implemented area-based transfer methods
---------------------------------------
1. Intensive variable

       Y_tj = sum_i(X_ti * A_ij) / sum_i(A_ij)

   The denominator is recalculated per time row using only contributors with a
   valid numerical value. Therefore missing values are excluded and the
   remaining weights are renormalized.

2. Extensive variable, covered-share rule

       Y_tj = sum_i(X_ti * A_ij / A_i)

   A partially covered input polygon transfers only the spatially covered share
   of its value.

Spatial intersections are computed once and reused for all temporal rows.
Alternatively, an existing polygon intersection layer can be supplied.

Runtime
-------
The PyQGIS runtime is discovered dynamically; no installation path is hard-coded.
"""

import csv
import gc
import math
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


from qgis_runtime import configure_from_argv

QGIS_PREFIX = configure_from_argv()

from qgis.PyQt.QtCore import Qt, QVariant, pyqtSignal  # noqa: E402
from qgis.PyQt.QtWidgets import (  # noqa: E402
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (  # noqa: E402
    NULL,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsProviderRegistry,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsProjectionSelectionDialog  # noqa: E402


NUMERIC_VARIANT_TYPES = {
    QVariant.Int,
    QVariant.UInt,
    QVariant.LongLong,
    QVariant.ULongLong,
    QVariant.Double,
}


@dataclass
class GridRecord:
    uid: str
    raw_uid: Any
    geometry: QgsGeometry
    area_m2: float


@dataclass
class OutputRecord:
    uid: str
    raw_uid: Any
    geometry: QgsGeometry
    area_m2: float


@dataclass(frozen=True)
class Contribution:
    input_uid: str
    input_column_index: int
    intersection_area_m2: float
    extensive_coefficient: float


@dataclass(frozen=True)
class ExistingIntersectionConfiguration:
    source: str
    input_uid_field: str
    output_uid_field: str
    area_field: Optional[str]


@dataclass(frozen=True)
class ProcessingConfiguration:
    input_source: str
    output_source: str
    input_uid_field: str
    output_uid_field: str
    input_area_field: Optional[str]
    output_area_field: Optional[str]
    target_crs: str
    intersection_mode: str
    existing_intersection: Optional[ExistingIntersectionConfiguration]
    csv_path: str
    delimiter_mode: str
    key_column: str
    variable_type: str
    strict_uid_coverage: bool
    output_csv: str
    save_weights_csv: bool


class PolygonLayerSelector(QGroupBox):
    layer_changed = pyqtSignal()

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(title, parent)
        self.layer: Optional[QgsVectorLayer] = None
        self.source_uri = ""

        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.browse_button = QPushButton("Browse")
        self.uid_combo = QComboBox()
        self.area_combo = QComboBox()
        self.info_label = QLabel("No layer selected")
        self.info_label.setWordWrap(True)

        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_button)

        form = QFormLayout(self)
        form.addRow("Polygon layer", path_row)
        form.addRow("UID field", self.uid_combo)
        form.addRow("Area field", self.area_combo)
        form.addRow("Layer information", self.info_label)

        self.browse_button.clicked.connect(self._browse)

    def _browse(self) -> None:
        source = choose_polygon_source(self, f"Select {self.title().lower()}")
        if source:
            self.load_layer(source)

    def load_layer(self, source: str) -> None:
        layer = QgsVectorLayer(source, layer_display_name(source), "ogr")
        if not layer.isValid():
            QMessageBox.critical(self, "Invalid layer", f"Could not load:\n{source}")
            return
        if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
            QMessageBox.critical(self, "Invalid geometry", "The selected layer is not polygonal.")
            return

        self.layer = layer
        self.source_uri = source
        self.path_edit.setText(source)
        self.uid_combo.clear()
        self.area_combo.clear()
        self.area_combo.addItem(COMPUTE_AREA_LABEL, None)
        for field in layer.fields():
            self.uid_combo.addItem(field.name(), field.name())
            if is_numeric_field(field):
                self.area_combo.addItem(field.name(), field.name())

        crs_text = layer.crs().authid() or layer.crs().description() or "Undefined CRS"
        self.info_label.setText(
            f"{layer.featureCount():,} features, {len(layer.fields())} fields, CRS: {crs_text}"
        )
        self.layer_changed.emit()

    def selected_uid_field(self) -> str:
        return str(self.uid_combo.currentData() or "")

    def selected_area_field(self) -> Optional[str]:
        value = self.area_combo.currentData()
        return str(value) if value else None


class CsvSelector(QGroupBox):
    csv_changed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Temporal matrix CSV", parent)
        self.headers: List[str] = []
        self.detected_delimiter = ","

        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.browse_button = QPushButton("Browse")
        self.delimiter_combo = QComboBox()
        self.delimiter_combo.addItems(
            [DELIMITER_AUTO, DELIMITER_COMMA, DELIMITER_SEMICOLON, DELIMITER_TAB]
        )
        self.key_combo = QComboBox()
        self.info_label = QLabel("No CSV selected")
        self.info_label.setWordWrap(True)

        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_button)

        form = QFormLayout(self)
        form.addRow("CSV file", path_row)
        form.addRow("Delimiter", self.delimiter_combo)
        form.addRow("Time/key column", self.key_combo)
        form.addRow("CSV information", self.info_label)

        self.browse_button.clicked.connect(self._browse)
        self.delimiter_combo.currentIndexChanged.connect(self._reload_header)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select temporal matrix CSV",
            "",
            "CSV files (*.csv *.txt);;All files (*.*)",
        )
        if path:
            self.path_edit.setText(path)
            self._reload_header()

    def _reload_header(self, *_args) -> None:
        path = self.path_edit.text().strip()
        self.headers = []
        self.key_combo.clear()
        if not path:
            self.info_label.setText("No CSV selected")
            self.csv_changed.emit()
            return
        try:
            delimiter = resolve_delimiter(path, self.delimiter_combo.currentText())
            self.detected_delimiter = delimiter
            headers, row_count = inspect_csv(path, delimiter)
        except Exception as exc:
            self.info_label.setText(f"Could not read CSV: {exc}")
            self.csv_changed.emit()
            return

        self.headers = headers
        for header in headers:
            self.key_combo.addItem(header, header)
        preferred = self.key_combo.findText("DATE_STR")
        if preferred >= 0:
            self.key_combo.setCurrentIndex(preferred)
        elif self.key_combo.count() > 0:
            self.key_combo.setCurrentIndex(0)

        delimiter_name = {
            ",": "comma",
            ";": "semicolon",
            "\t": "tab",
        }.get(delimiter, repr(delimiter))
        self.info_label.setText(
            f"{row_count:,} data rows, {len(headers):,} columns, delimiter: {delimiter_name}"
        )
        self.csv_changed.emit()

    def selected_key_column(self) -> str:
        return str(self.key_combo.currentData() or "")


class ExistingIntersectionPanel(QGroupBox):
    changed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Existing intersection layer", parent)
        self.layer: Optional[QgsVectorLayer] = None
        self.source_uri = ""

        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.browse_button = QPushButton("Browse")
        self.input_uid_combo = QComboBox()
        self.output_uid_combo = QComboBox()
        self.area_combo = QComboBox()
        self.info_label = QLabel("No intersection layer selected")
        self.info_label.setWordWrap(True)

        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_button)

        form = QFormLayout(self)
        form.addRow("Intersection layer", path_row)
        form.addRow("Input UID field", self.input_uid_combo)
        form.addRow("Output UID field", self.output_uid_combo)
        form.addRow("Intersection area field", self.area_combo)
        form.addRow("Layer information", self.info_label)

        self.browse_button.clicked.connect(self._browse)
        self.input_uid_combo.currentIndexChanged.connect(lambda _x: self.changed.emit())
        self.output_uid_combo.currentIndexChanged.connect(lambda _x: self.changed.emit())
        self.area_combo.currentIndexChanged.connect(lambda _x: self.changed.emit())

    def _browse(self) -> None:
        source = choose_polygon_source(self, "Select existing intersection layer")
        if source:
            self.load_layer(source)

    def load_layer(self, source: str) -> None:
        layer = QgsVectorLayer(source, layer_display_name(source), "ogr")
        if not layer.isValid():
            QMessageBox.warning(self, "Invalid intersection", f"Could not load:\n{source}")
            return
        if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
            QMessageBox.warning(self, "Invalid intersection", "The layer must be polygonal.")
            return

        self.layer = layer
        self.source_uri = source
        self.path_edit.setText(source)
        self.input_uid_combo.clear()
        self.output_uid_combo.clear()
        self.area_combo.clear()
        self.area_combo.addItem(COMPUTE_AREA_LABEL, None)
        for field in layer.fields():
            self.input_uid_combo.addItem(field.name(), field.name())
            self.output_uid_combo.addItem(field.name(), field.name())
            if is_numeric_field(field):
                self.area_combo.addItem(field.name(), field.name())
        crs_text = layer.crs().authid() or layer.crs().description() or "Undefined CRS"
        self.info_label.setText(
            f"{layer.featureCount():,} features, {len(layer.fields())} fields, CRS: {crs_text}"
        )
        self.changed.emit()

    def configuration(self) -> Optional[ExistingIntersectionConfiguration]:
        if self.layer is None or not self.layer.isValid():
            return None
        input_uid = str(self.input_uid_combo.currentData() or "")
        output_uid = str(self.output_uid_combo.currentData() or "")
        if not input_uid or not output_uid:
            return None
        area_value = self.area_combo.currentData()
        return ExistingIntersectionConfiguration(
            source=self.source_uri,
            input_uid_field=input_uid,
            output_uid_field=output_uid,
            area_field=str(area_value) if area_value else None,
        )


class TemporalGridReprojectorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Temporal Grid Reprojector")
        self.resize(1120, 900)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        intro = QLabel(
            "Reproject a temporal CSV matrix whose spatial columns correspond to input-grid "
            "polygon UIDs. Spatial intersections are calculated once and reused for all rows."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        grids = QGridLayout()
        self.input_selector = PolygonLayerSelector("Input grid", self)
        self.output_selector = PolygonLayerSelector("Output grid", self)
        grids.addWidget(self.input_selector, 0, 0)
        grids.addWidget(self.output_selector, 0, 1)
        root.addLayout(grids)

        crs_group = QGroupBox("Common processing CRS")
        crs_form = QFormLayout(crs_group)
        self.crs_edit = QLineEdit("EPSG:32632")
        self.crs_button = QPushButton("Select CRS")
        crs_row = QHBoxLayout()
        crs_row.addWidget(self.crs_edit, 1)
        crs_row.addWidget(self.crs_button)
        crs_form.addRow("Target CRS", crs_row)
        crs_note = QLabel(
            "Input, output, and existing intersection geometries are transformed to this CRS "
            "before geometry-based area calculation."
        )
        crs_note.setWordWrap(True)
        crs_form.addRow("", crs_note)
        root.addWidget(crs_group)

        intersection_group = QGroupBox("Intersection source")
        intersection_form = QFormLayout(intersection_group)
        self.intersection_mode_combo = QComboBox()
        self.intersection_mode_combo.addItems([INTERSECTION_COMPUTE, INTERSECTION_EXISTING])
        intersection_form.addRow("Mode", self.intersection_mode_combo)
        self.existing_intersection_panel = ExistingIntersectionPanel(self)
        self.existing_intersection_panel.setVisible(False)
        intersection_form.addRow(self.existing_intersection_panel)
        root.addWidget(intersection_group)

        self.csv_selector = CsvSelector(self)
        root.addWidget(self.csv_selector)

        method_group = QGroupBox("Transfer method")
        method_form = QFormLayout(method_group)
        self.variable_type_combo = QComboBox()
        self.variable_type_combo.addItems([VARIABLE_INTENSIVE, VARIABLE_EXTENSIVE])
        self.strict_checkbox = QCheckBox(
            "Require exact UID coverage between input-grid polygons and CSV spatial columns"
        )
        self.strict_checkbox.setChecked(True)
        intensive_note = QLabel(
            "Intensive: normalized intersection-area weighted mean. Extensive: covered-share "
            "allocation using intersection area / input polygon area."
        )
        intensive_note.setWordWrap(True)
        method_form.addRow("Variable type", self.variable_type_combo)
        method_form.addRow("UID validation", self.strict_checkbox)
        method_form.addRow("", intensive_note)
        root.addWidget(method_group)

        output_group = QGroupBox("Output")
        output_form = QFormLayout(output_group)
        self.output_csv_edit = QLineEdit()
        self.output_csv_button = QPushButton("Browse")
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_csv_edit, 1)
        output_row.addWidget(self.output_csv_button)
        self.save_weights_checkbox = QCheckBox("Save spatial transfer weights as an additional CSV")
        self.save_weights_checkbox.setChecked(True)
        output_form.addRow("Output temporal CSV", output_row)
        output_form.addRow("Diagnostics", self.save_weights_checkbox)
        root.addWidget(output_group)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(180)
        root.addWidget(self.log)

        buttons = QHBoxLayout()
        self.run_button = QPushButton("Run temporal grid transfer")
        self.run_button.setEnabled(False)
        self.close_button = QPushButton("Close")
        buttons.addStretch(1)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.close_button)
        root.addLayout(buttons)

        self.input_selector.layer_changed.connect(self._state_changed)
        self.output_selector.layer_changed.connect(self._state_changed)
        self.csv_selector.csv_changed.connect(self._state_changed)
        self.existing_intersection_panel.changed.connect(self._state_changed)
        self.intersection_mode_combo.currentIndexChanged.connect(self._intersection_mode_changed)
        self.crs_button.clicked.connect(self._select_crs)
        self.output_csv_button.clicked.connect(self._browse_output_csv)
        self.run_button.clicked.connect(self._run)
        self.close_button.clicked.connect(self.close)

    def _intersection_mode_changed(self, _index: int) -> None:
        use_existing = self.intersection_mode_combo.currentText() == INTERSECTION_EXISTING
        self.existing_intersection_panel.setVisible(use_existing)
        self._state_changed()

    def _state_changed(self, *_args) -> None:
        ready = (
            self.input_selector.layer is not None
            and self.output_selector.layer is not None
            and bool(self.csv_selector.headers)
            and bool(self.csv_selector.selected_key_column())
        )
        if self.intersection_mode_combo.currentText() == INTERSECTION_EXISTING:
            ready = ready and self.existing_intersection_panel.configuration() is not None
        self.run_button.setEnabled(bool(ready))

    def _select_crs(self) -> None:
        dialog = QgsProjectionSelectionDialog(self)
        current = QgsCoordinateReferenceSystem()
        current.createFromUserInput(self.crs_edit.text().strip() or "EPSG:32632")
        if current.isValid():
            dialog.setCrs(current)
        if dialog.exec_() != QDialog.Accepted:
            return
        selected = dialog.crs()
        if selected.isValid():
            self.crs_edit.setText(selected.authid() or selected.toWkt())

    def _browse_output_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Select output temporal CSV",
            self.output_csv_edit.text().strip() or "reprojected_temporal_data.csv",
            "CSV files (*.csv)",
        )
        if path:
            if not path.lower().endswith(".csv"):
                path += ".csv"
            self.output_csv_edit.setText(path)

    def _build_configuration(self) -> ProcessingConfiguration:
        input_layer = self.input_selector.layer
        output_layer = self.output_selector.layer
        if input_layer is None or output_layer is None:
            raise ValueError("Select both input and output polygon grids.")

        input_uid = self.input_selector.selected_uid_field()
        output_uid = self.output_selector.selected_uid_field()
        if not input_uid or not output_uid:
            raise ValueError("Select the UID field for both grids.")

        csv_path = self.csv_selector.path_edit.text().strip()
        key_column = self.csv_selector.selected_key_column()
        if not csv_path or not key_column:
            raise ValueError("Select the temporal CSV and its time/key column.")

        output_csv = self.output_csv_edit.text().strip()
        if not output_csv:
            source_path = Path(csv_path)
            output_csv = str(source_path.with_name(source_path.stem + "_reprojected.csv"))
            self.output_csv_edit.setText(output_csv)
        if not output_csv.lower().endswith(".csv"):
            output_csv += ".csv"
            self.output_csv_edit.setText(output_csv)

        source_csv_path = Path(csv_path).expanduser().resolve()
        output_csv_path = Path(output_csv).expanduser().resolve()
        if os.path.normcase(str(source_csv_path)) == os.path.normcase(str(output_csv_path)):
            raise ValueError(
                "The output temporal CSV must be different from the input CSV. "
                "Choose a different output filename to prevent source-data loss."
            )

        target_crs_text = self.crs_edit.text().strip() or "EPSG:32632"
        target_crs = QgsCoordinateReferenceSystem()
        if not target_crs.createFromUserInput(target_crs_text) or not target_crs.isValid():
            raise ValueError("Define a valid processing CRS, for example EPSG:32632.")

        intersection_mode = self.intersection_mode_combo.currentText()
        existing = None
        if intersection_mode == INTERSECTION_EXISTING:
            existing = self.existing_intersection_panel.configuration()
            if existing is None:
                raise ValueError("Configure a valid existing intersection layer.")

        return ProcessingConfiguration(
            input_source=self.input_selector.source_uri,
            output_source=self.output_selector.source_uri,
            input_uid_field=input_uid,
            output_uid_field=output_uid,
            input_area_field=self.input_selector.selected_area_field(),
            output_area_field=self.output_selector.selected_area_field(),
            target_crs=target_crs.authid() or target_crs_text,
            intersection_mode=intersection_mode,
            existing_intersection=existing,
            csv_path=csv_path,
            delimiter_mode=self.csv_selector.delimiter_combo.currentText(),
            key_column=key_column,
            variable_type=self.variable_type_combo.currentText(),
            strict_uid_coverage=self.strict_checkbox.isChecked(),
            output_csv=output_csv,
            save_weights_csv=self.save_weights_checkbox.isChecked(),
        )

    def _run(self) -> None:
        try:
            config = self._build_configuration()
        except Exception as exc:
            QMessageBox.warning(self, "Configuration error", str(exc))
            return

        output_path = Path(config.output_csv)
        if output_path.exists():
            answer = QMessageBox.question(
                self,
                "Overwrite output",
                f"The output CSV already exists and will be replaced:\n{output_path}\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        self.run_button.setEnabled(False)
        self.progress.setValue(0)
        self.log.clear()
        try:
            processor = TemporalGridReprojector(config, self._log, self._set_progress)
            summary = processor.run()
            self.progress.setValue(100)
            self._log("Processing completed successfully.")
            self._log(
                f"Rows written: {summary['rows']:,}; input columns used: "
                f"{summary['mapped_input_columns']:,}; output columns: {summary['output_columns']:,}."
            )
            QMessageBox.information(
                self,
                "Completed",
                "Temporal grid transfer completed.\n\n"
                f"Output CSV: {config.output_csv}\n"
                f"Report: {summary['report_log']}"
                + (
                    f"\nSpatial weights: {summary['weights_csv']}"
                    if summary.get("weights_csv")
                    else ""
                ),
            )
        except Exception as exc:
            self._log(traceback.format_exc())
            QMessageBox.critical(self, "Processing error", str(exc))
        finally:
            self._state_changed()

    def _log(self, message: str) -> None:
        self.log.append(message)
        QgsApplication.processEvents()

    def _set_progress(self, value: int) -> None:
        self.progress.setValue(max(0, min(100, int(value))))
        QgsApplication.processEvents()


class TemporalGridReprojector:
    def __init__(self, config: ProcessingConfiguration, log_callback, progress_callback) -> None:
        self.config = config
        self.log = log_callback
        self.progress = progress_callback
        self.context = QgsProject.instance().transformContext()

        self.input_layer = QgsVectorLayer(config.input_source, "input_grid", "ogr")
        self.output_layer = QgsVectorLayer(config.output_source, "output_grid", "ogr")
        if not self.input_layer.isValid() or not self.output_layer.isValid():
            raise RuntimeError("The input or output grid could not be loaded.")
        if not self.input_layer.crs().isValid() or not self.output_layer.crs().isValid():
            raise RuntimeError("Both grids must have a valid CRS.")

        self.target_crs = QgsCoordinateReferenceSystem()
        if not self.target_crs.createFromUserInput(config.target_crs) or not self.target_crs.isValid():
            raise RuntimeError(f"Invalid target CRS: {config.target_crs}")

        self.distance_area = QgsDistanceArea()
        self.distance_area.setSourceCrs(self.target_crs, self.context)
        ellipsoid = self.target_crs.ellipsoidAcronym()
        self.distance_area.setEllipsoid(ellipsoid if ellipsoid else "WGS84")

        self.warnings: List[str] = []

    def run(self) -> Dict[str, Any]:
        delimiter = resolve_delimiter(self.config.csv_path, self.config.delimiter_mode)
        headers, row_count = inspect_csv(self.config.csv_path, delimiter)
        if self.config.key_column not in headers:
            raise ValueError(f"CSV key column not found: {self.config.key_column}")

        self.log("Validating grid UIDs and preparing geometries.")
        input_records = self._prepare_input_records()
        output_records = self._prepare_output_records()
        self.progress(10)

        data_column_mapping, unmatched_columns, missing_input_uids = self._map_csv_columns(
            headers, input_records
        )

        if self.config.strict_uid_coverage:
            problems = []
            if unmatched_columns:
                problems.append(
                    "CSV columns without a matching input-grid UID: "
                    + ", ".join(unmatched_columns[:20])
                    + (" ..." if len(unmatched_columns) > 20 else "")
                )
            if missing_input_uids:
                problems.append(
                    "Input-grid UIDs without a CSV column: "
                    + ", ".join(missing_input_uids[:20])
                    + (" ..." if len(missing_input_uids) > 20 else "")
                )
            if problems:
                raise ValueError("Strict UID coverage failed. " + " | ".join(problems))
        else:
            if unmatched_columns:
                self._warn(
                    f"{len(unmatched_columns):,} CSV column(s) were ignored because they do not "
                    "match an input-grid UID."
                )
            if missing_input_uids:
                self._warn(
                    f"{len(missing_input_uids):,} input-grid polygon(s) have no CSV column and "
                    "will not contribute to the temporal transfer."
                )

        if not data_column_mapping:
            raise ValueError("No CSV spatial columns could be matched to input-grid UIDs.")

        input_column_index_by_uid = {
            input_uid: column_index for column_index, input_uid in data_column_mapping.items()
        }

        self.log("Building the reusable spatial transfer relationships.")
        if self.config.intersection_mode == INTERSECTION_EXISTING:
            contributions, relationship_rows = self._build_from_existing_intersections(
                input_records, output_records, input_column_index_by_uid
            )
        else:
            contributions, relationship_rows = self._build_from_computed_intersections(
                input_records, output_records, input_column_index_by_uid
            )
        self.progress(35)

        total_relationships = sum(len(items) for items in contributions.values())
        if total_relationships == 0:
            raise ValueError(
                "No positive-area spatial relationship exists between mapped input cells and "
                "output polygons."
            )

        output_uids = [record.uid for record in output_records]
        uncovered_outputs = [uid for uid in output_uids if not contributions.get(uid)]
        if uncovered_outputs:
            self._warn(
                f"{len(uncovered_outputs):,} output polygon(s) have no mapped positive-area "
                "input contribution. Their temporal values will be blank."
            )

        weights_csv = None
        if self.config.save_weights_csv:
            weights_csv = self._write_weights_csv(relationship_rows)

        self.log(
            f"Applying {total_relationships:,} reusable spatial relationships to "
            f"{row_count:,} temporal rows."
        )
        rows_written, null_cells = self._process_csv(
            delimiter,
            headers,
            row_count,
            output_uids,
            contributions,
        )
        self.progress(95)

        report_log = self._write_report(
            delimiter=delimiter,
            input_records=input_records,
            output_records=output_records,
            mapped_count=len(data_column_mapping),
            unmatched_columns=unmatched_columns,
            missing_input_uids=missing_input_uids,
            relationships=total_relationships,
            uncovered_outputs=uncovered_outputs,
            rows_written=rows_written,
            null_cells=null_cells,
            weights_csv=weights_csv,
        )

        return {
            "rows": rows_written,
            "mapped_input_columns": len(data_column_mapping),
            "output_columns": len(output_uids),
            "relationships": total_relationships,
            "report_log": str(report_log),
            "weights_csv": str(weights_csv) if weights_csv else None,
        }

    def _prepare_input_records(self) -> Dict[str, GridRecord]:
        self._validate_uid_field(self.input_layer, self.config.input_uid_field, "input")
        records: Dict[str, GridRecord] = {}
        for feature in self.input_layer.getFeatures():
            uid = uid_to_text(feature[self.config.input_uid_field])
            geometry = geometry_in_crs(
                feature.geometry(), self.input_layer.crs(), self.target_crs, self.context
            )
            area = self._feature_area(
                feature, geometry, self.config.input_area_field, "input"
            )
            if uid in records:
                raise ValueError(f"Duplicate canonical input UID: {uid}")
            records[uid] = GridRecord(uid, feature[self.config.input_uid_field], geometry, area)
        return records

    def _prepare_output_records(self) -> List[OutputRecord]:
        self._validate_uid_field(self.output_layer, self.config.output_uid_field, "output")
        records: List[OutputRecord] = []
        seen: set = set()
        for feature in self.output_layer.getFeatures():
            uid = uid_to_text(feature[self.config.output_uid_field])
            if uid in seen:
                raise ValueError(f"Duplicate canonical output UID: {uid}")
            seen.add(uid)
            geometry = geometry_in_crs(
                feature.geometry(), self.output_layer.crs(), self.target_crs, self.context
            )
            area = self._feature_area(
                feature, geometry, self.config.output_area_field, "output"
            )
            records.append(OutputRecord(uid, feature[self.config.output_uid_field], geometry, area))
        return records

    def _validate_uid_field(self, layer: QgsVectorLayer, field_name: str, label: str) -> None:
        names = set(layer.fields().names())
        if field_name not in names:
            raise ValueError(f"Missing {label} UID field: {field_name}")
        seen_raw: set = set()
        for feature in layer.getFeatures():
            value = feature[field_name]
            if is_null(value):
                raise ValueError(f"The {label} UID field contains null values: {field_name}")
            key = uid_to_text(value)
            if key in seen_raw:
                raise ValueError(f"The {label} UID field is not unique after normalization: {key}")
            seen_raw.add(key)

    def _feature_area(
        self,
        feature: QgsFeature,
        geometry: QgsGeometry,
        area_field: Optional[str],
        label: str,
    ) -> float:
        if area_field:
            value = to_finite_float(feature[area_field])
            if value is None or value <= 0:
                raise ValueError(
                    f"Feature {feature.id()} in the {label} grid has invalid area in "
                    f"field '{area_field}'. Existing area fields must contain positive m2."
                )
            return value
        area = float(self.distance_area.measureArea(geometry))
        if not math.isfinite(area) or area <= 0:
            raise ValueError(f"Feature {feature.id()} in the {label} grid has invalid geometry area.")
        return area

    def _map_csv_columns(
        self,
        headers: Sequence[str],
        input_records: Dict[str, GridRecord],
    ) -> Tuple[Dict[int, str], List[str], List[str]]:
        key_index = headers.index(self.config.key_column)
        input_aliases = build_uid_alias_lookup(input_records.keys())
        mapping: Dict[int, str] = {}
        unmatched: List[str] = []
        used_uids: set = set()

        for index, header in enumerate(headers):
            if index == key_index:
                continue
            matched = resolve_header_uid(header, input_records, input_aliases)
            if matched is None:
                unmatched.append(header)
                continue
            if matched in used_uids:
                raise ValueError(
                    f"More than one CSV column maps to input UID '{matched}'. "
                    f"The duplicate mapping includes column '{header}'."
                )
            used_uids.add(matched)
            mapping[index] = matched

        missing = sorted(set(input_records) - used_uids, key=natural_sort_key)
        return mapping, unmatched, missing

    def _build_from_computed_intersections(
        self,
        input_records: Dict[str, GridRecord],
        output_records: Sequence[OutputRecord],
        input_column_index_by_uid: Dict[str, int],
    ) -> Tuple[Dict[str, List[Contribution]], List[Tuple[Any, ...]]]:
        spatial_index = QgsSpatialIndex()
        fid_to_uid: Dict[int, str] = {}
        for fid, record in enumerate(input_records.values(), start=1):
            feature = QgsFeature()
            feature.setId(fid)
            feature.setGeometry(record.geometry)
            spatial_index.addFeature(feature)
            fid_to_uid[fid] = record.uid

        contributions: Dict[str, List[Contribution]] = {record.uid: [] for record in output_records}
        relationship_rows: List[Tuple[Any, ...]] = []
        output_count = len(output_records)

        for position, output_record in enumerate(output_records, start=1):
            for fid in spatial_index.intersects(output_record.geometry.boundingBox()):
                input_uid = fid_to_uid.get(int(fid))
                if input_uid is None:
                    continue
                input_record = input_records[input_uid]
                if not output_record.geometry.intersects(input_record.geometry):
                    continue
                intersection = output_record.geometry.intersection(input_record.geometry)
                if intersection.isNull() or intersection.isEmpty():
                    continue
                area = float(self.distance_area.measureArea(intersection))
                if not math.isfinite(area) or area <= 0:
                    continue

                column_index = input_column_index_by_uid.get(input_uid)
                if column_index is None:
                    continue
                extensive_coefficient = area / input_record.area_m2
                contributions[output_record.uid].append(
                    Contribution(input_uid, column_index, area, extensive_coefficient)
                )
                relationship_rows.append(
                    (
                        output_record.uid,
                        input_uid,
                        area,
                        input_record.area_m2,
                        output_record.area_m2,
                        area / output_record.area_m2,
                        extensive_coefficient,
                    )
                )

            if output_count:
                self.progress(10 + int(25 * position / output_count))
        return contributions, relationship_rows

    def _build_from_existing_intersections(
        self,
        input_records: Dict[str, GridRecord],
        output_records: Sequence[OutputRecord],
        input_column_index_by_uid: Dict[str, int],
    ) -> Tuple[Dict[str, List[Contribution]], List[Tuple[Any, ...]]]:
        config = self.config.existing_intersection
        if config is None:
            raise RuntimeError("Existing intersection mode is not configured.")
        layer = QgsVectorLayer(config.source, "existing_intersections", "ogr")
        if not layer.isValid():
            raise ValueError("The existing intersection layer could not be loaded.")
        if not layer.crs().isValid():
            raise ValueError("The existing intersection layer has no valid CRS.")

        names = set(layer.fields().names())
        required = {config.input_uid_field, config.output_uid_field}
        if config.area_field:
            required.add(config.area_field)
        missing_fields = sorted(required - names)
        if missing_fields:
            raise ValueError(
                "The existing intersection layer is missing fields: " + ", ".join(missing_fields)
            )

        output_by_uid = {record.uid: record for record in output_records}
        contributions: Dict[str, List[Contribution]] = {record.uid: [] for record in output_records}
        relationship_rows: List[Tuple[Any, ...]] = []
        pair_areas: Dict[Tuple[str, str], float] = {}
        feature_count = int(layer.featureCount())

        for position, feature in enumerate(layer.getFeatures(), start=1):
            input_uid = uid_to_text(feature[config.input_uid_field])
            output_uid = uid_to_text(feature[config.output_uid_field])
            input_record = input_records.get(input_uid)
            output_record = output_by_uid.get(output_uid)
            if input_record is None:
                raise ValueError(
                    f"Existing intersection references input UID '{input_uid}', which is absent "
                    "from the selected input grid."
                )
            if output_record is None:
                raise ValueError(
                    f"Existing intersection references output UID '{output_uid}', which is absent "
                    "from the selected output grid."
                )

            if config.area_field:
                area = to_finite_float(feature[config.area_field])
                if area is None or area <= 0:
                    raise ValueError(
                        f"Intersection feature {feature.id()} has invalid area in "
                        f"'{config.area_field}'."
                    )
            else:
                geometry = geometry_in_crs(
                    feature.geometry(), layer.crs(), self.target_crs, self.context
                )
                area = float(self.distance_area.measureArea(geometry))
                if not math.isfinite(area) or area <= 0:
                    raise ValueError(
                        f"Intersection feature {feature.id()} has invalid geometry area."
                    )

            pair = (output_uid, input_uid)
            pair_areas[pair] = pair_areas.get(pair, 0.0) + float(area)

            if feature_count and position % 100 == 0:
                self.progress(10 + int(18 * position / feature_count))

        pair_count = len(pair_areas)
        for position, ((output_uid, input_uid), area) in enumerate(pair_areas.items(), start=1):
            input_record = input_records[input_uid]
            output_record = output_by_uid[output_uid]
            column_index = input_column_index_by_uid.get(input_uid)
            if column_index is None:
                continue
            extensive_coefficient = area / input_record.area_m2
            contributions[output_uid].append(
                Contribution(input_uid, column_index, area, extensive_coefficient)
            )
            relationship_rows.append(
                (
                    output_uid,
                    input_uid,
                    area,
                    input_record.area_m2,
                    output_record.area_m2,
                    area / output_record.area_m2,
                    extensive_coefficient,
                )
            )
            if pair_count and position % 100 == 0:
                self.progress(28 + int(7 * position / pair_count))

        return contributions, relationship_rows

    def _process_csv(
        self,
        delimiter: str,
        headers: Sequence[str],
        row_count: int,
        output_uids: Sequence[str],
        contributions: Dict[str, List[Contribution]],
    ) -> Tuple[int, int]:
        output_path = Path(self.config.output_csv)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        key_index = headers.index(self.config.key_column)
        null_cells = 0
        rows_written = 0

        with open(self.config.csv_path, "r", newline="", encoding="utf-8-sig") as src, open(
            output_path, "w", newline="", encoding="utf-8"
        ) as dst:
            reader = csv.reader(src, delimiter=delimiter)
            writer = csv.writer(dst, delimiter=",")
            source_header = next(reader, None)
            if source_header is None:
                raise ValueError("The source CSV is empty.")
            if list(source_header) != list(headers):
                raise ValueError("The source CSV header changed between validation and processing.")

            writer.writerow([self.config.key_column, *output_uids])

            for row_number, row in enumerate(reader, start=1):
                if not row:
                    continue
                if len(row) != len(headers):
                    raise ValueError(
                        f"CSV row {row_number + 1} contains {len(row)} columns, expected "
                        f"{len(headers)}."
                    )
                key_value = row[key_index]
                out_values: List[str] = []

                for output_uid in output_uids:
                    items = contributions.get(output_uid, [])
                    if not items:
                        out_values.append("")
                        null_cells += 1
                        continue

                    if self.config.variable_type == VARIABLE_INTENSIVE:
                        numerator = 0.0
                        denominator = 0.0
                        for item in items:
                            value = parse_csv_float(row[item.input_column_index])
                            if value is None:
                                continue
                            numerator += value * item.intersection_area_m2
                            denominator += item.intersection_area_m2
                        if denominator > 0:
                            out_values.append(format_number(numerator / denominator))
                        else:
                            out_values.append("")
                            null_cells += 1
                    else:
                        total = 0.0
                        valid_count = 0
                        for item in items:
                            value = parse_csv_float(row[item.input_column_index])
                            if value is None:
                                continue
                            total += value * item.extensive_coefficient
                            valid_count += 1
                        if valid_count:
                            out_values.append(format_number(total))
                        else:
                            out_values.append("")
                            null_cells += 1

                writer.writerow([key_value, *out_values])
                rows_written += 1
                if row_count and rows_written % 25 == 0:
                    self.progress(35 + int(60 * rows_written / row_count))

        return rows_written, null_cells

    def _write_weights_csv(self, rows: Sequence[Tuple[Any, ...]]) -> Path:
        output_path = Path(self.config.output_csv)
        path = output_path.with_name(output_path.stem + "_spatial_weights.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "output_uid",
                    "input_uid",
                    "intersection_area_m2",
                    "input_area_m2",
                    "output_area_m2",
                    "output_area_share",
                    "input_area_share",
                ]
            )
            for row in rows:
                writer.writerow(row)
        return path

    def _write_report(
        self,
        delimiter: str,
        input_records: Dict[str, GridRecord],
        output_records: Sequence[OutputRecord],
        mapped_count: int,
        unmatched_columns: Sequence[str],
        missing_input_uids: Sequence[str],
        relationships: int,
        uncovered_outputs: Sequence[str],
        rows_written: int,
        null_cells: int,
        weights_csv: Optional[Path],
    ) -> Path:
        output_path = Path(self.config.output_csv)
        report_path = output_path.with_name(output_path.stem + "_report.log")
        lines = [
            "Temporal Grid Reprojector report",
            f"Input grid: {self.config.input_source}",
            f"Output grid: {self.config.output_source}",
            f"Input UID field: {self.config.input_uid_field}",
            f"Output UID field: {self.config.output_uid_field}",
            f"Target CRS: {self.target_crs.authid() or self.config.target_crs}",
            f"Intersection mode: {self.config.intersection_mode}",
            f"Source CSV: {self.config.csv_path}",
            f"Source delimiter: {repr(delimiter)}",
            f"Time/key column: {self.config.key_column}",
            f"Variable type: {self.config.variable_type}",
            f"Output CSV: {self.config.output_csv}",
            f"Input polygons: {len(input_records)}",
            f"Mapped CSV input columns: {mapped_count}",
            f"Output polygons/columns: {len(output_records)}",
            f"Spatial relationships used: {relationships}",
            f"Rows written: {rows_written}",
            f"Blank output cells produced: {null_cells}",
            f"Unmatched CSV columns: {len(unmatched_columns)}",
            f"Input UIDs without CSV columns: {len(missing_input_uids)}",
            f"Output polygons without mapped contributors: {len(uncovered_outputs)}",
        ]
        if weights_csv:
            lines.append(f"Spatial weights CSV: {weights_csv}")
        if unmatched_columns:
            lines.append("Unmatched CSV columns: " + ", ".join(unmatched_columns))
        if missing_input_uids:
            lines.append("Missing input UIDs: " + ", ".join(missing_input_uids))
        if uncovered_outputs:
            lines.append("Uncovered output UIDs: " + ", ".join(uncovered_outputs))
        if self.warnings:
            lines.append("")
            lines.append("Warnings")
            lines.append("--------")
            lines.extend(self.warnings)
        else:
            lines.append("")
            lines.append("No warnings were generated.")
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report_path

    def _warn(self, message: str) -> None:
        self.warnings.append(message)
        self.log("Warning: " + message)


def choose_polygon_source(parent: QWidget, title: str) -> Optional[str]:
    """Browse a vector dataset and, for containers, select a polygon sublayer."""
    path, _ = QFileDialog.getOpenFileName(
        parent,
        title,
        "",
        "Polygon vector data (*.shp *.gpkg);;All files (*.*)",
    )
    if not path:
        return None
    if not path.lower().endswith(".gpkg"):
        return path

    # QGIS >= 3.22 can enumerate container sublayers. If unavailable, fall back
    # to the GeoPackage path, which still works for many single-layer packages.
    try:
        details = QgsProviderRegistry.instance().querySublayers(path)
        polygon_details = []
        for detail in details:
            try:
                if QgsWkbTypes.geometryType(detail.wkbType()) == QgsWkbTypes.PolygonGeometry:
                    polygon_details.append(detail)
            except Exception:
                continue
        if not polygon_details:
            return path
        if len(polygon_details) == 1:
            return polygon_details[0].uri()
        labels = [detail.name() for detail in polygon_details]
        selected, ok = QInputDialog.getItem(
            parent,
            "Select GeoPackage layer",
            "Polygon layer",
            labels,
            0,
            False,
        )
        if not ok:
            return None
        index = labels.index(selected)
        return polygon_details[index].uri()
    except Exception:
        return path


def layer_display_name(source: str) -> str:
    base = Path(source.split("|")[0]).stem
    if "|layername=" in source:
        layer_name = source.split("|layername=", 1)[1].split("|", 1)[0]
        return layer_name or base
    return base


def resolve_delimiter(path: str, mode: str) -> str:
    if mode == DELIMITER_COMMA:
        return ","
    if mode == DELIMITER_SEMICOLON:
        return ";"
    if mode == DELIMITER_TAB:
        return "\t"
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        sample = f.read(65536)
    if not sample:
        raise ValueError("The CSV file is empty.")
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        return dialect.delimiter
    except csv.Error:
        return ","


def inspect_csv(path: str, delimiter: str) -> Tuple[List[str], int]:
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f, delimiter=delimiter)
        header = next(reader, None)
        if not header:
            raise ValueError("The CSV has no header row.")
        if len(set(header)) != len(header):
            duplicates = sorted({name for name in header if header.count(name) > 1})
            raise ValueError("Duplicate CSV column names: " + ", ".join(duplicates))
        row_count = sum(1 for row in reader if row)
    return [str(item).strip() for item in header], row_count


def build_uid_alias_lookup(uids: Iterable[str]) -> Dict[str, Optional[str]]:
    aliases: Dict[str, Optional[str]] = {}
    for uid in uids:
        for alias in uid_aliases(uid):
            existing = aliases.get(alias)
            if existing is None and alias in aliases:
                continue
            if existing is not None and existing != uid:
                aliases[alias] = None
            elif alias not in aliases:
                aliases[alias] = uid
    return aliases


def resolve_header_uid(
    header: str,
    input_records: Dict[str, GridRecord],
    alias_lookup: Dict[str, Optional[str]],
) -> Optional[str]:
    raw = str(header).strip()
    if raw in input_records:
        return raw
    for alias in uid_aliases(raw):
        candidate = alias_lookup.get(alias)
        if candidate:
            return candidate
    return None


def uid_aliases(value: Any) -> List[str]:
    raw = str(value).strip()
    aliases = [raw]
    try:
        numeric = float(raw)
        if math.isfinite(numeric) and numeric.is_integer():
            canonical = str(int(numeric))
            if canonical not in aliases:
                aliases.append(canonical)
    except (TypeError, ValueError):
        pass
    return aliases


def uid_to_text(value: Any) -> str:
    if is_null(value):
        raise ValueError("UID values cannot be null.")
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Invalid numerical UID: {value}")
        if value.is_integer():
            return str(int(value))
        return format(value, ".15g")
    return str(value).strip()


def natural_sort_key(value: str) -> Tuple[int, Any]:
    try:
        return (0, float(value))
    except ValueError:
        return (1, value)


def geometry_in_crs(
    geometry: QgsGeometry,
    source_crs: QgsCoordinateReferenceSystem,
    target_crs: QgsCoordinateReferenceSystem,
    context,
) -> QgsGeometry:
    result = QgsGeometry(geometry)
    if result.isNull() or result.isEmpty():
        raise ValueError("A polygon feature has an empty geometry.")
    if source_crs != target_crs:
        transform = QgsCoordinateTransform(source_crs, target_crs, context)
        result.transform(transform)
    return result


def parse_csv_float(value: Any) -> Optional[float]:
    text = str(value).strip()
    if text == "" or text.casefold() in {"na", "nan", "null", "none", "nodata"}:
        return None
    try:
        numeric = float(text)
    except ValueError:
        return None
    return numeric if math.isfinite(numeric) else None


def format_number(value: float) -> str:
    return format(value, ".15g")


def is_numeric_field(field) -> bool:
    try:
        if field.isNumeric():
            return True
    except (AttributeError, TypeError):
        pass
    try:
        if field.type() in NUMERIC_VARIANT_TYPES:
            return True
    except (AttributeError, TypeError):
        pass
    type_name = (field.typeName() or "").strip().lower()
    tokens = (
        "int",
        "integer",
        "smallint",
        "bigint",
        "real",
        "float",
        "double",
        "numeric",
        "decimal",
    )
    return any(token in type_name for token in tokens)


def is_null(value: Any) -> bool:
    if value is None:
        return True
    try:
        return value == NULL
    except Exception:
        return False


def to_finite_float(value: Any) -> Optional[float]:
    if is_null(value):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def main() -> int:
    QgsApplication.setPrefixPath(QGIS_PREFIX, True)
    qgis_argv = [os.fsencode(argument) for argument in sys.argv]
    app = QgsApplication(qgis_argv, True)
    app.initQgis()
    try:
        window = TemporalGridReprojectorWindow()
        window.show()
        if hasattr(app, "exec_"):
            return app.exec_()
        return app.exec()
    finally:
        app.exitQgis()


if __name__ == "__main__":
    sys.exit(main())
