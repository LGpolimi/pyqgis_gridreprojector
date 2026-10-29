from __future__ import annotations

"""
Standalone PyQGIS application for transferring numerical variables from an
input polygon grid to an output polygon grid.

Weighting strategies
--------------------
1. Area weighting
   Intensive targets:
       Y_op = sum(X_i * A_ij) / sum(A_ij)
   Extensive targets, covered-share rule:
       VV_ij = TV_i * A_ij / A_i
       V_op = sum(VV_ij)

2. Alternative weighting
   A nonnegative fragment weight W_ij is calculated or read for every
   input/output intersection.

   Intensive targets:
       Y_op = sum(X_i * W_ij) / sum(W_ij)

   Extensive targets:
       VV_ij = TV_i * W_ij / sum_j(W_ij)
       V_op = sum_i(VV_ij)

   The extensive-target alternative-weight formula preserves the complete
   input value across the available weighted intersections.

Alternative weight sources
--------------------------
Intensive weight variable:
- input grid only: W_ij = Z_i * A_ij
- output grid only: W_ij = Z_j * A_ij
- both grids: W_ij = ((Z_i + Z_j) / 2) * A_ij
- existing intersection layer: W_ij = Z_ij * A_ij

Extensive weight variable:
- directly usable only when available for each intersection relationship
- when available only on the input grid, output grid, or both grids, the
  application can generate an intersection template containing a blank
  fragment-weight field. The transfer is not executed in that session.

Outputs
-------
A GeoPackage containing:
- grid_intersections
- reprojected_output, except in extensive-weight template mode

Existing area fields are assumed to be expressed in square metres. When area
is computed, QgsDistanceArea is used and areas are stored in square metres.
"""

import gc
import math
import os
import re
import sys
import traceback
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


from qgis_runtime import configure_from_argv

QGIS_PREFIX = configure_from_argv()

from qgis.PyQt.QtCore import Qt, QVariant, pyqtSignal  # noqa: E402
from qgis.PyQt.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
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
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsProject,
    QgsProviderRegistry,
    QgsSpatialIndex,
    QgsVectorFileWriter,
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


@dataclass(frozen=True)
class VariableSpec:
    source_field: str
    category: str
    output_field: str
    intersection_source_field: str
    intersection_value_field: Optional[str]


@dataclass
class InputRecord:
    fid: int
    uid: Any
    geometry: QgsGeometry
    area_m2: float
    values: Dict[str, Optional[float]]
    weight_value: Optional[float]


@dataclass(frozen=True)
class AlternativeWeightConfiguration:
    variable_type: str
    availability: str
    input_field: Optional[str] = None
    output_field: Optional[str] = None
    intersection_path: Optional[str] = None
    intersection_uid_field: Optional[str] = None
    intersection_input_uid_field: Optional[str] = None
    intersection_weight_field: Optional[str] = None
    template_only: bool = False
    template_weight_field: str = "fragment_weight"


@dataclass(frozen=True)
class IntersectionSourceConfiguration:
    mode: str
    source_path: Optional[str] = None
    intersection_uid_field: Optional[str] = None
    input_uid_field: Optional[str] = None
    output_uid_field: Optional[str] = None
    area_field: Optional[str] = None


@dataclass
class OutputRecord:
    uid: Any
    feature: QgsFeature
    geometry: QgsGeometry
    area_m2: float
    weight_value: Optional[float]


@dataclass(frozen=True)
class ProcessingConfiguration:
    input_path: str
    output_path: str
    input_uid_field: str
    output_uid_field: str
    intersection_uid_field: str
    input_area_field: Optional[str]
    output_area_field: Optional[str]
    variables: Tuple[Tuple[str, str], ...]
    output_field_prefix: str
    destination_gpkg: str
    target_crs: str
    intersection_source: IntersectionSourceConfiguration
    weighting_strategy: str
    alternative_weight: Optional[AlternativeWeightConfiguration]


class GridLayerSelector(QGroupBox):
    layer_changed = pyqtSignal()

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(title, parent)
        self.layer: Optional[QgsVectorLayer] = None

        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.browse_button = QPushButton("Browse")
        self.uid_combo = QComboBox()
        self.area_combo = QComboBox()
        self.info_label = QLabel("No layer selected")
        self.info_label.setWordWrap(True)

        path_layout = QHBoxLayout()
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(self.browse_button)

        layout = QFormLayout(self)
        layout.addRow("Polygon layer", path_layout)
        layout.addRow("UID field", self.uid_combo)
        layout.addRow("Area field", self.area_combo)
        layout.addRow("Layer information", self.info_label)

        self.browse_button.clicked.connect(self._browse)

    def _browse(self) -> None:
        source = choose_polygon_source(self, f"Select {self.title().lower()}")
        if source:
            self.load_layer(source)

    def load_layer(self, source: str) -> None:
        layer = QgsVectorLayer(source, Path(source.split("|")[0]).stem, "ogr")
        if not layer.isValid():
            QMessageBox.critical(
                self,
                "Invalid layer",
                "Could not load the selected source. For a multi-layer GeoPackage, "
                "use a provider URI ending with |layername=<layer name>.",
            )
            return

        if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
            QMessageBox.critical(self, "Invalid geometry", "The selected layer is not polygonal.")
            return

        self.layer = layer
        self.path_edit.setText(source)
        self._populate_fields()

        crs_text = layer.crs().authid() or layer.crs().description() or "Undefined CRS"
        self.info_label.setText(
            f"{layer.featureCount():,} features, {len(layer.fields())} fields, CRS: {crs_text}"
        )
        # Do not call the parent widget directly. Qt may reparent this selector
        # when it is inserted into the main layout, so self.parent() is not
        # guaranteed to be GridTransferWindow. Emit an explicit signal instead.
        self.layer_changed.emit()

    def _populate_fields(self) -> None:
        assert self.layer is not None
        self.uid_combo.clear()
        self.area_combo.clear()
        self.area_combo.addItem(COMPUTE_AREA_LABEL, None)

        for field in self.layer.fields():
            self.uid_combo.addItem(field.name(), field.name())
            if is_numeric_field(field):
                self.area_combo.addItem(field.name(), field.name())

    def selected_uid_field(self) -> str:
        return str(self.uid_combo.currentData() or "")

    def selected_area_field(self) -> Optional[str]:
        data = self.area_combo.currentData()
        return str(data) if data else None


class AlternativeWeightDialog(QDialog):
    """Collect and validate the alternative-weight configuration."""

    def __init__(
        self,
        input_layer: QgsVectorLayer,
        output_layer: QgsVectorLayer,
        has_extensive_targets: bool,
        has_intensive_targets: bool,
        previous: Optional[AlternativeWeightConfiguration] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Configure alternative weighting")
        self.resize(760, 610)
        self.input_layer = input_layer
        self.output_layer = output_layer
        self.has_extensive_targets = has_extensive_targets
        self.has_intensive_targets = has_intensive_targets
        self.intersection_layer: Optional[QgsVectorLayer] = None
        self._result_config: Optional[AlternativeWeightConfiguration] = None

        root = QVBoxLayout(self)

        intro = QLabel(
            "An alternative weight must provide a nonnegative numerical weight for every "
            "input/output intersection used by the transfer. Select the mathematical type "
            "of the weighting variable and where it is currently available."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        definition_group = QGroupBox("Weight definition")
        definition_form = QFormLayout(definition_group)
        self.type_combo = QComboBox()
        self.type_combo.addItems([WEIGHT_INTENSIVE, WEIGHT_EXTENSIVE])
        self.availability_combo = QComboBox()
        self.availability_combo.addItems(
            [SOURCE_INPUT, SOURCE_OUTPUT, SOURCE_BOTH, SOURCE_INTERSECTION]
        )
        definition_form.addRow("Weight variable type", self.type_combo)
        definition_form.addRow("Variable availability", self.availability_combo)
        root.addWidget(definition_group)

        source_group = QGroupBox("Source fields")
        source_form = QFormLayout(source_group)
        self.input_field_combo = QComboBox()
        self.output_field_combo = QComboBox()
        populate_numeric_combo(self.input_field_combo, input_layer)
        populate_numeric_combo(self.output_field_combo, output_layer)
        source_form.addRow("Input-grid weight field", self.input_field_combo)
        source_form.addRow("Output-grid weight field", self.output_field_combo)
        root.addWidget(source_group)
        self.source_group = source_group

        intersection_group = QGroupBox("Existing intersection weight layer")
        intersection_form = QFormLayout(intersection_group)
        self.intersection_path_edit = QLineEdit()
        self.intersection_path_edit.setPlaceholderText(
            "path/to/weights.gpkg|layername=grid_intersections"
        )
        self.intersection_browse_button = QPushButton("Browse")
        self.intersection_load_button = QPushButton("Load")
        path_row = QHBoxLayout()
        path_row.addWidget(self.intersection_path_edit, 1)
        path_row.addWidget(self.intersection_browse_button)
        path_row.addWidget(self.intersection_load_button)
        self.intersection_uid_combo = QComboBox()
        self.intersection_input_uid_combo = QComboBox()
        self.intersection_weight_combo = QComboBox()
        intersection_form.addRow("Intersection layer source", path_row)
        intersection_form.addRow("Combined intersection UID field", self.intersection_uid_combo)
        intersection_form.addRow("Input UID field", self.intersection_input_uid_combo)
        intersection_form.addRow("Weight field", self.intersection_weight_combo)
        root.addWidget(intersection_group)
        self.intersection_group = intersection_group

        template_group = QGroupBox("Extensive-weight preparation")
        template_layout = QVBoxLayout(template_group)
        template_text = QLabel(
            "An extensive polygon-level variable is not directly a fragment weight. "
            "When it is not already available on the intersection layer, this session can "
            "create grid_intersections with the selected source values and a blank numerical "
            "field. Populate that field externally, then rerun using Existing intersection layer."
        )
        template_text.setWordWrap(True)
        template_layout.addWidget(template_text)
        self.template_checkbox = QCheckBox(
            "Create the intersection weight template as the output of this session"
        )
        self.template_checkbox.setChecked(True)
        template_layout.addWidget(self.template_checkbox)
        template_form = QFormLayout()
        self.template_field_edit = QLineEdit("fragment_weight")
        template_form.addRow("Blank fragment-weight field name", self.template_field_edit)
        template_layout.addLayout(template_form)
        root.addWidget(template_group)
        self.template_group = template_group

        self.conditions_label = QLabel()
        self.conditions_label.setWordWrap(True)
        self.conditions_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.conditions_label)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        root.addWidget(self.buttons)

        self.type_combo.currentIndexChanged.connect(self._update_state)
        self.availability_combo.currentIndexChanged.connect(self._update_state)
        self.input_field_combo.currentIndexChanged.connect(self._update_state)
        self.output_field_combo.currentIndexChanged.connect(self._update_state)
        self.intersection_browse_button.clicked.connect(self._browse_intersection_layer)
        self.intersection_load_button.clicked.connect(self._load_intersection_layer)
        self.intersection_uid_combo.currentIndexChanged.connect(self._update_state)
        self.intersection_input_uid_combo.currentIndexChanged.connect(self._update_state)
        self.intersection_weight_combo.currentIndexChanged.connect(self._update_state)
        self.template_checkbox.stateChanged.connect(self._update_state)
        self.template_field_edit.textChanged.connect(self._update_state)
        self.buttons.accepted.connect(self._accept_configuration)
        self.buttons.rejected.connect(self.reject)

        if previous is not None:
            self._restore_previous(previous)
        self._update_state()

    def configuration(self) -> Optional[AlternativeWeightConfiguration]:
        return self._result_config

    def _restore_previous(self, previous: AlternativeWeightConfiguration) -> None:
        set_combo_text(self.type_combo, previous.variable_type)
        set_combo_text(self.availability_combo, previous.availability)
        set_combo_data(self.input_field_combo, previous.input_field)
        set_combo_data(self.output_field_combo, previous.output_field)
        self.template_checkbox.setChecked(previous.template_only)
        self.template_field_edit.setText(previous.template_weight_field)
        if previous.intersection_path:
            self.intersection_path_edit.setText(previous.intersection_path)
            self._load_intersection_layer(silent=True)
            set_combo_data(self.intersection_uid_combo, previous.intersection_uid_field)
            set_combo_data(
                self.intersection_input_uid_combo,
                previous.intersection_input_uid_field,
            )
            set_combo_data(
                self.intersection_weight_combo,
                previous.intersection_weight_field,
            )

    def _browse_intersection_layer(self) -> None:
        source = choose_polygon_source(self, "Select intersection weight layer")
        if not source:
            return
        self.intersection_path_edit.setText(source)
        self._load_intersection_layer()

    def _load_intersection_layer(self, silent: bool = False) -> None:
        source = self.intersection_path_edit.text().strip()
        self.intersection_layer = None
        self.intersection_uid_combo.clear()
        self.intersection_input_uid_combo.clear()
        self.intersection_weight_combo.clear()

        if not source:
            self._update_state()
            return

        layer = QgsVectorLayer(source, "intersection_weights", "ogr")
        if not layer.isValid():
            if not silent:
                QMessageBox.warning(
                    self,
                    "Invalid intersection layer",
                    "The intersection layer could not be loaded. For GeoPackage data, include "
                    "|layername=<layer name> in the source URI.",
                )
            self._update_state()
            return

        self.intersection_layer = layer
        for field in layer.fields():
            self.intersection_uid_combo.addItem(field.name(), field.name())
            self.intersection_input_uid_combo.addItem(field.name(), field.name())
            if is_numeric_field(field):
                self.intersection_weight_combo.addItem(field.name(), field.name())
        self._update_state()

    def _update_state(self, *_args) -> None:
        weight_type = self.type_combo.currentText()
        availability = self.availability_combo.currentText()

        needs_input = availability in (SOURCE_INPUT, SOURCE_BOTH)
        needs_output = availability in (SOURCE_OUTPUT, SOURCE_BOTH)
        needs_intersection = availability == SOURCE_INTERSECTION
        template_mode = weight_type == WEIGHT_EXTENSIVE and not needs_intersection

        self.source_group.setVisible(not needs_intersection)
        self.input_field_combo.setEnabled(needs_input)
        self.output_field_combo.setEnabled(needs_output)
        self.intersection_group.setVisible(needs_intersection)
        self.template_group.setVisible(template_mode)

        if weight_type == WEIGHT_INTENSIVE:
            conditions = (
                "Required conditions: the selected field or fields must be numerical, finite, "
                "and nonnegative for every relevant polygon. Input-only values are assumed "
                "constant within input polygons; output-only values are assumed constant within "
                "output polygons; values from both grids are averaged on each intersection. "
                "An intersection-level intensive value is multiplied by intersection area."
            )
        else:
            conditions = (
                "Required conditions: an extensive weight is directly usable only when a "
                "nonnegative fragment total is available for every intersection relationship. "
                "Polygon-level extensive values require inference before transfer."
            )
        if (
            weight_type == WEIGHT_INTENSIVE
            and availability == SOURCE_OUTPUT
            and self.has_intensive_targets
        ):
            conditions += (
                " Warning: for intensive target variables, an intensive weight available "
                "only on the output grid is constant across all intersections belonging "
                "to the same output polygon. It therefore cancels during normalization "
                "and produces the same result as ordinary area weighting for those targets."
            )
        self.conditions_label.setText(conditions)

        valid, message = self._validate_current(show_values=False)
        self.status_label.setText(message)
        self.status_label.setStyleSheet(
            "color: #176b2c;" if valid else "color: #9b1c1c;"
        )
        ok_button = self.buttons.button(QDialogButtonBox.Ok)
        if ok_button is not None:
            ok_button.setEnabled(valid)

    def _validate_current(self, show_values: bool) -> Tuple[bool, str]:
        weight_type = self.type_combo.currentText()
        availability = self.availability_combo.currentText()

        if availability in (SOURCE_INPUT, SOURCE_BOTH):
            if not self.input_field_combo.currentData():
                return False, "Select a numerical weight field from the input grid."
        if availability in (SOURCE_OUTPUT, SOURCE_BOTH):
            if not self.output_field_combo.currentData():
                return False, "Select a numerical weight field from the output grid."

        if availability == SOURCE_INTERSECTION:
            if self.intersection_layer is None or not self.intersection_layer.isValid():
                return False, "Load a valid existing intersection layer."
            if not self.intersection_uid_combo.currentData():
                return False, "Select the combined intersection UID field."
            if not self.intersection_input_uid_combo.currentData():
                return False, "Select the input UID field in the intersection layer."
            if not self.intersection_weight_combo.currentData():
                return False, "Select a numerical intersection weight field."
            if weight_type == WEIGHT_EXTENSIVE:
                return True, (
                    "Configuration valid. The selected field will be used directly as the "
                    "extensive fragment weight."
                )
            return True, (
                "Configuration valid. The selected field will be treated as an intensive "
                "intersection value and multiplied by intersection area."
            )

        if weight_type == WEIGHT_EXTENSIVE:
            if not self.template_checkbox.isChecked():
                return False, (
                    "The selected polygon-level extensive variable cannot be used directly. "
                    "Enable intersection-template creation or select Existing intersection layer."
                )
            field_name = self.template_field_edit.text().strip()
            if not field_name:
                return False, "Define the blank fragment-weight field name."
            if not is_valid_field_name(field_name):
                return False, (
                    "The fragment-weight field name must contain only letters, digits, and "
                    "underscores, and must not start with a digit."
                )
            return True, (
                "Configuration valid for preparation mode. The session will create only "
                "grid_intersections with a blank fragment-weight field."
            )

        return True, "Configuration valid for direct intensive alternative weighting."

    def _accept_configuration(self) -> None:
        valid, message = self._validate_current(show_values=True)
        if not valid:
            QMessageBox.warning(self, "Incomplete weighting configuration", message)
            return

        availability = self.availability_combo.currentText()
        weight_type = self.type_combo.currentText()
        if (
            weight_type == WEIGHT_INTENSIVE
            and availability == SOURCE_OUTPUT
            and self.has_intensive_targets
        ):
            answer = QMessageBox.warning(
                self,
                "Ineffective weighting for intensive targets",
                "The selected output-grid intensive weight will cancel during the "
                "normalized calculation of intensive target variables. Those variables "
                "will have the same result as area weighting. Continue with this "
                "configuration?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self._result_config = AlternativeWeightConfiguration(
            variable_type=weight_type,
            availability=availability,
            input_field=str(self.input_field_combo.currentData() or "") or None,
            output_field=str(self.output_field_combo.currentData() or "") or None,
            intersection_path=(
                self.intersection_path_edit.text().strip()
                if availability == SOURCE_INTERSECTION
                else None
            ),
            intersection_uid_field=(
                str(self.intersection_uid_combo.currentData() or "") or None
                if availability == SOURCE_INTERSECTION
                else None
            ),
            intersection_input_uid_field=(
                str(self.intersection_input_uid_combo.currentData() or "") or None
                if availability == SOURCE_INTERSECTION
                else None
            ),
            intersection_weight_field=(
                str(self.intersection_weight_combo.currentData() or "") or None
                if availability == SOURCE_INTERSECTION
                else None
            ),
            template_only=(weight_type == WEIGHT_EXTENSIVE and availability != SOURCE_INTERSECTION),
            template_weight_field=self.template_field_edit.text().strip() or "fragment_weight",
        )
        self.accept()


class VariableSelectionDialog(QDialog):
    """Large, searchable editor for target variable selection and classification."""

    def __init__(
        self,
        variable_states: Sequence[Tuple[str, str, bool, str]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select variables to transfer")
        self.resize(1120, 720)

        root = QVBoxLayout(self)

        search_row = QHBoxLayout()
        search_label = QLabel("Search field name")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Enter any part of a variable name, matching is case-insensitive"
        )
        self.clear_search_button = QPushButton("Clear search")
        search_row.addWidget(search_label)
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.clear_search_button)
        root.addLayout(search_row)

        self.match_label = QLabel()
        root.addWidget(self.match_label)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Use", "Input field", "Field type", "Variable category"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        root.addWidget(self.table, 1)

        controls = QHBoxLayout()
        self.select_visible_button = QPushButton("Select visible")
        self.clear_visible_button = QPushButton("Clear visible")
        self.set_intensive_button = QPushButton("Set selected or checked as intensive")
        self.set_extensive_button = QPushButton("Set selected or checked as extensive")
        controls.addWidget(self.select_visible_button)
        controls.addWidget(self.clear_visible_button)
        controls.addStretch(1)
        controls.addWidget(self.set_intensive_button)
        controls.addWidget(self.set_extensive_button)
        root.addLayout(controls)

        self.action_label = QLabel(
            "Category buttons apply to highlighted rows. If no rows are highlighted, "
            "they apply to checked visible rows."
        )
        self.action_label.setWordWrap(True)
        root.addWidget(self.action_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._populate(variable_states)

        self.search_edit.textChanged.connect(self._filter_rows)
        self.clear_search_button.clicked.connect(self.search_edit.clear)
        self.select_visible_button.clicked.connect(lambda: self._set_visible_checked(True))
        self.clear_visible_button.clicked.connect(lambda: self._set_visible_checked(False))
        self.set_intensive_button.clicked.connect(
            lambda: self._set_category("Intensive")
        )
        self.set_extensive_button.clicked.connect(
            lambda: self._set_category("Extensive")
        )

    def _populate(
        self,
        variable_states: Sequence[Tuple[str, str, bool, str]],
    ) -> None:
        self.table.setRowCount(len(variable_states))
        for row, (name, type_name, checked, category) in enumerate(variable_states):
            use_item = QTableWidgetItem()
            use_item.setFlags(
                Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable
            )
            use_item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            self.table.setItem(row, 0, use_item)

            name_item = QTableWidgetItem(name)
            name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 1, name_item)

            type_item = QTableWidgetItem(type_name)
            type_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 2, type_item)

            category_item = QTableWidgetItem(
                category if category in {"Intensive", "Extensive"} else "Intensive"
            )
            category_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 3, category_item)

        self._update_match_label()

    def _filter_rows(self, text: str) -> None:
        query = text.strip().casefold()
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 1)
            name = name_item.text().casefold() if name_item is not None else ""
            self.table.setRowHidden(row, bool(query) and query not in name)
        self._update_match_label()

    def _update_match_label(self) -> None:
        visible = sum(
            1 for row in range(self.table.rowCount()) if not self.table.isRowHidden(row)
        )
        checked_visible = sum(
            1
            for row in range(self.table.rowCount())
            if not self.table.isRowHidden(row)
            and self.table.item(row, 0) is not None
            and self.table.item(row, 0).checkState() == Qt.Checked
        )
        self.match_label.setText(
            f"Visible variables: {visible:,} of {self.table.rowCount():,}; "
            f"checked among visible: {checked_visible:,}."
        )

    def _set_visible_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        changed = 0
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(state)
                changed += 1
        self._update_match_label()
        self.action_label.setText(
            f"Updated the selection state of {changed:,} visible variable(s)."
        )

    def _target_rows(self) -> List[int]:
        selected_rows = sorted(
            {
                index.row()
                for index in self.table.selectionModel().selectedRows()
                if not self.table.isRowHidden(index.row())
            }
        )
        if selected_rows:
            return selected_rows

        checked_rows = []
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                checked_rows.append(row)
        return checked_rows

    def _set_category(self, category: str) -> None:
        rows = self._target_rows()
        if not rows:
            self.action_label.setText(
                "No highlighted or checked visible variables were found."
            )
            return

        for row in rows:
            category_item = self.table.item(row, 3)
            if category_item is not None:
                category_item.setText(category)

        self.table.viewport().update()
        self.action_label.setText(
            f"Set {len(rows):,} variable(s) to {category}."
        )

    def variable_states(self) -> Dict[str, Tuple[bool, str]]:
        states: Dict[str, Tuple[bool, str]] = {}
        for row in range(self.table.rowCount()):
            use_item = self.table.item(row, 0)
            name_item = self.table.item(row, 1)
            category_item = self.table.item(row, 3)
            if use_item is None or name_item is None or category_item is None:
                continue
            states[name_item.text()] = (
                use_item.checkState() == Qt.Checked,
                category_item.text(),
            )
        return states


class GridTransferWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Polygon Grid Transfer")
        self.resize(1080, 900)
        self._intersection_uid_manually_edited = False
        self.alternative_weight_config: Optional[AlternativeWeightConfiguration] = None
        self.existing_intersection_layer: Optional[QgsVectorLayer] = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        intro = QLabel(
            "Transfer numerical variables from an input polygon grid to an output polygon grid. "
            "Each target variable is classified as intensive or extensive. Area weighting and "
            "configured alternative weighting are supported."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        grids_layout = QGridLayout()
        self.input_selector = GridLayerSelector("Input grid", self)
        self.output_selector = GridLayerSelector("Output grid", self)
        grids_layout.addWidget(self.input_selector, 0, 0)
        grids_layout.addWidget(self.output_selector, 0, 1)
        root.addLayout(grids_layout)

        crs_group = QGroupBox("Common processing CRS")
        crs_form = QFormLayout(crs_group)
        self.target_crs_edit = QLineEdit("EPSG:32632")
        self.target_crs_button = QPushButton("Select CRS")
        crs_row = QHBoxLayout()
        crs_row.addWidget(self.target_crs_edit, 1)
        crs_row.addWidget(self.target_crs_button)
        crs_form.addRow("Target CRS", crs_row)
        crs_note = QLabel(
            "Input, output, and uploaded intersection geometries are transformed to this "
            "CRS before area measurement and transfer. The default is EPSG:32632."
        )
        crs_note.setWordWrap(True)
        crs_form.addRow("", crs_note)
        root.addWidget(crs_group)

        intersection_group = QGroupBox("Intersection source")
        intersection_form = QFormLayout(intersection_group)
        self.intersection_mode_combo = QComboBox()
        self.intersection_mode_combo.addItems([INTERSECTION_COMPUTE, INTERSECTION_EXISTING])
        intersection_form.addRow("Mode", self.intersection_mode_combo)

        self.existing_intersection_panel = QWidget()
        existing_form = QFormLayout(self.existing_intersection_panel)
        existing_form.setContentsMargins(0, 0, 0, 0)
        self.existing_intersection_path_edit = QLineEdit()
        self.existing_intersection_path_edit.setPlaceholderText(
            "path/to/intersections.gpkg|layername=grid_intersections"
        )
        self.existing_intersection_browse_button = QPushButton("Browse")
        self.existing_intersection_load_button = QPushButton("Load")
        existing_path_row = QHBoxLayout()
        existing_path_row.addWidget(self.existing_intersection_path_edit, 1)
        existing_path_row.addWidget(self.existing_intersection_browse_button)
        existing_path_row.addWidget(self.existing_intersection_load_button)
        self.existing_intersection_uid_combo = QComboBox()
        self.existing_intersection_input_uid_combo = QComboBox()
        self.existing_intersection_output_uid_combo = QComboBox()
        self.existing_intersection_area_combo = QComboBox()
        self.existing_intersection_info = QLabel("No intersection layer loaded")
        self.existing_intersection_info.setWordWrap(True)
        existing_form.addRow("Intersection layer", existing_path_row)
        existing_form.addRow("Intersection UID field", self.existing_intersection_uid_combo)
        existing_form.addRow("Input UID field", self.existing_intersection_input_uid_combo)
        existing_form.addRow("Output UID field", self.existing_intersection_output_uid_combo)
        existing_form.addRow("Intersection area field", self.existing_intersection_area_combo)
        existing_form.addRow("Layer information", self.existing_intersection_info)
        intersection_form.addRow(self.existing_intersection_panel)
        root.addWidget(intersection_group)
        self.existing_intersection_panel.setVisible(False)

        variables_group = QGroupBox("Variables to transfer")
        variables_layout = QVBoxLayout(variables_group)
        self.variable_table = QTableWidget(0, 4)
        self.variable_table.setHorizontalHeaderLabels(
            ["Use", "Input field", "Field type", "Variable category"]
        )
        self.variable_table.verticalHeader().setVisible(False)
        self.variable_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.variable_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        header = self.variable_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        variables_layout.addWidget(self.variable_table)

        variable_buttons = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.clear_all_button = QPushButton("Clear all")
        self.set_intensive_button = QPushButton("Set selected or checked as intensive")
        self.set_extensive_button = QPushButton("Set selected or checked as extensive")
        self.open_variable_selector_button = QPushButton("Open larger variable selector")
        variable_buttons.addWidget(self.select_all_button)
        variable_buttons.addWidget(self.clear_all_button)
        variable_buttons.addWidget(self.open_variable_selector_button)
        variable_buttons.addStretch(1)
        variable_buttons.addWidget(self.set_intensive_button)
        variable_buttons.addWidget(self.set_extensive_button)
        variables_layout.addLayout(variable_buttons)
        root.addWidget(variables_group, 1)

        weighting_group = QGroupBox("Weighting strategy")
        weighting_form = QFormLayout(weighting_group)
        self.weighting_combo = QComboBox()
        self.weighting_combo.addItems([WEIGHT_AREA, WEIGHT_ALTERNATIVE])
        self.configure_weight_button = QPushButton("Configure alternative weighting")
        self.configure_weight_button.setEnabled(False)
        self.weighting_summary = QLabel(
            "Area based: intensive targets use a normalized area-weighted mean; "
            "extensive targets use the covered-share rule."
        )
        self.weighting_summary.setWordWrap(True)
        weighting_form.addRow("Method", self.weighting_combo)
        weighting_form.addRow("Configuration", self.configure_weight_button)
        weighting_form.addRow("Current setup", self.weighting_summary)
        root.addWidget(weighting_group)

        output_group = QGroupBox("Output")
        output_layout = QFormLayout(output_group)
        self.intersection_uid_edit = QLineEdit()
        self.intersection_uid_edit.setPlaceholderText(
            "Select the input and output UID fields to generate the default name"
        )
        output_layout.addRow("Intersection UID field name", self.intersection_uid_edit)

        self.output_prefix_edit = QLineEdit("tr")
        self.output_prefix_edit.setPlaceholderText(
            "Prefix for reprojected fields, for example tr"
        )
        prefix_note = QLabel(
            "The prefix is added before every reprojected field name. Leave it blank "
            "to use the source field name, subject to automatic collision handling."
        )
        prefix_note.setWordWrap(True)
        output_layout.addRow("Reprojected field prefix", self.output_prefix_edit)
        output_layout.addRow("", prefix_note)

        self.destination_edit = QLineEdit()
        self.destination_button = QPushButton("Browse")
        destination_row = QHBoxLayout()
        destination_row.addWidget(self.destination_edit, 1)
        destination_row.addWidget(self.destination_button)
        output_layout.addRow("Destination GeoPackage", destination_row)
        self.layers_created_label = QLabel("grid_intersections and reprojected_output")
        output_layout.addRow("Layers created", self.layers_created_label)
        root.addWidget(output_group)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(155)
        root.addWidget(self.log)

        action_layout = QHBoxLayout()
        self.run_button = QPushButton("Run grid transfer")
        self.run_button.setEnabled(False)
        self.close_button = QPushButton("Close")
        action_layout.addStretch(1)
        action_layout.addWidget(self.run_button)
        action_layout.addWidget(self.close_button)
        root.addLayout(action_layout)

        self.input_selector.layer_changed.connect(self.layer_selection_changed)
        self.output_selector.layer_changed.connect(self.layer_selection_changed)

        self.select_all_button.clicked.connect(lambda: self._set_all_variables(True))
        self.clear_all_button.clicked.connect(lambda: self._set_all_variables(False))
        self.set_intensive_button.clicked.connect(lambda: self._set_selected_category("Intensive"))
        self.set_extensive_button.clicked.connect(lambda: self._set_selected_category("Extensive"))
        self.open_variable_selector_button.clicked.connect(self._open_variable_selector)
        self.input_selector.uid_combo.currentIndexChanged.connect(
            lambda _index: self._update_default_intersection_uid_field()
        )
        self.output_selector.uid_combo.currentIndexChanged.connect(
            lambda _index: self._update_default_intersection_uid_field()
        )
        self.intersection_uid_edit.textEdited.connect(
            self._mark_intersection_uid_field_as_manual
        )
        self.weighting_combo.currentIndexChanged.connect(self._weighting_strategy_changed)
        self.configure_weight_button.clicked.connect(self._configure_alternative_weighting)
        self.target_crs_button.clicked.connect(self._select_target_crs)
        self.intersection_mode_combo.currentIndexChanged.connect(self._intersection_mode_changed)
        self.existing_intersection_browse_button.clicked.connect(
            self._browse_existing_intersection_layer
        )
        self.existing_intersection_load_button.clicked.connect(
            self._load_existing_intersection_layer
        )
        for combo in (
            self.existing_intersection_uid_combo,
            self.existing_intersection_input_uid_combo,
            self.existing_intersection_output_uid_combo,
            self.existing_intersection_area_combo,
        ):
            combo.currentIndexChanged.connect(lambda _index: self._update_run_state())
        self.destination_button.clicked.connect(self._browse_destination)
        self.run_button.clicked.connect(self._run)
        self.close_button.clicked.connect(self.close)

    def layer_selection_changed(self) -> None:
        if self.input_selector.layer is not None:
            self._populate_variable_table(self.input_selector.layer)
            self.statusBar().showMessage(
                f"Input grid loaded: {len(self.input_selector.layer.fields())} total field(s), "
                f"{self.variable_table.rowCount()} numerical field(s) listed.",
                12000,
            )
        self._update_default_intersection_uid_field()
        self._refresh_existing_intersection_field_defaults()
        if self.weighting_combo.currentText() == WEIGHT_ALTERNATIVE:
            self.alternative_weight_config = None
            self.weighting_summary.setText(
                "Input or output grid changed. Reconfigure the alternative weighting."
            )
        self._update_run_state()

    def _update_run_state(self) -> None:
        layers_ready = (
            self.input_selector.layer is not None and self.output_selector.layer is not None
        )
        weighting_ready = (
            self.weighting_combo.currentText() == WEIGHT_AREA
            or self.alternative_weight_config is not None
        )
        intersection_ready = True
        if self.intersection_mode_combo.currentText() == INTERSECTION_EXISTING:
            intersection_ready = bool(
                self.existing_intersection_layer is not None
                and self.existing_intersection_layer.isValid()
                and self.existing_intersection_uid_combo.currentData()
                and self.existing_intersection_input_uid_combo.currentData()
                and self.existing_intersection_output_uid_combo.currentData()
            )
        self.run_button.setEnabled(layers_ready and weighting_ready and intersection_ready)

    def _mark_intersection_uid_field_as_manual(self, _text: str) -> None:
        self._intersection_uid_manually_edited = True

    def _update_default_intersection_uid_field(self) -> None:
        if self._intersection_uid_manually_edited:
            return
        input_uid = self.input_selector.selected_uid_field()
        output_uid = self.output_selector.selected_uid_field()
        self.intersection_uid_edit.setText(
            f"{output_uid}_{input_uid}" if input_uid and output_uid else ""
        )
        self._refresh_existing_intersection_field_defaults()

    def _select_target_crs(self) -> None:
        dialog = QgsProjectionSelectionDialog(self)
        current = QgsCoordinateReferenceSystem()
        current.createFromUserInput(self.target_crs_edit.text().strip() or "EPSG:32632")
        if current.isValid():
            dialog.setCrs(current)
        if dialog.exec_() != QDialog.Accepted:
            return
        selected = dialog.crs()
        if selected.isValid():
            self.target_crs_edit.setText(selected.authid() or selected.toWkt())

    def _intersection_mode_changed(self, _index: int) -> None:
        use_existing = self.intersection_mode_combo.currentText() == INTERSECTION_EXISTING
        self.existing_intersection_panel.setVisible(use_existing)
        self._update_run_state()

    def _browse_existing_intersection_layer(self) -> None:
        source = choose_polygon_source(self, "Select existing intersection layer")
        if not source:
            return
        self.existing_intersection_path_edit.setText(source)
        self._load_existing_intersection_layer()

    def _load_existing_intersection_layer(self) -> None:
        source = self.existing_intersection_path_edit.text().strip()
        self.existing_intersection_layer = None
        for combo in (
            self.existing_intersection_uid_combo,
            self.existing_intersection_input_uid_combo,
            self.existing_intersection_output_uid_combo,
            self.existing_intersection_area_combo,
        ):
            combo.clear()
        self.existing_intersection_area_combo.addItem(COMPUTE_AREA_LABEL, None)

        if not source:
            self.existing_intersection_info.setText("No intersection layer loaded")
            self._update_run_state()
            return

        layer = QgsVectorLayer(source, "existing_intersections", "ogr")
        if not layer.isValid():
            QMessageBox.warning(
                self,
                "Invalid intersection layer",
                "The selected intersection layer could not be loaded. For a multi-layer "
                "GeoPackage, enter a provider URI ending with |layername=<layer name>.",
            )
            self.existing_intersection_info.setText("Invalid intersection layer")
            self._update_run_state()
            return
        if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
            QMessageBox.warning(
                self,
                "Invalid intersection geometry",
                "The existing intersection layer must contain polygon geometries.",
            )
            self.existing_intersection_info.setText("The loaded layer is not polygonal")
            self._update_run_state()
            return

        self.existing_intersection_layer = layer
        for field in layer.fields():
            self.existing_intersection_uid_combo.addItem(field.name(), field.name())
            self.existing_intersection_input_uid_combo.addItem(field.name(), field.name())
            self.existing_intersection_output_uid_combo.addItem(field.name(), field.name())
            if is_numeric_field(field):
                self.existing_intersection_area_combo.addItem(field.name(), field.name())
        crs_text = layer.crs().authid() or layer.crs().description() or "Undefined CRS"
        self.existing_intersection_info.setText(
            f"{layer.featureCount():,} features, {len(layer.fields())} fields, CRS: {crs_text}"
        )
        self._refresh_existing_intersection_field_defaults()
        self._update_run_state()

    def _refresh_existing_intersection_field_defaults(self) -> None:
        if self.existing_intersection_layer is None:
            return
        set_combo_data(
            self.existing_intersection_uid_combo,
            self.intersection_uid_edit.text().strip(),
        )
        set_combo_data(
            self.existing_intersection_input_uid_combo,
            self.input_selector.selected_uid_field(),
        )
        set_combo_data(
            self.existing_intersection_output_uid_combo,
            self.output_selector.selected_uid_field(),
        )

    def _populate_variable_table(self, layer: QgsVectorLayer) -> None:
        previous = self._selected_variable_categories()
        numeric_fields = [field for field in layer.fields() if is_numeric_field(field)]

        self.variable_table.clearContents()
        self.variable_table.setRowCount(len(numeric_fields))
        self.variable_table.setToolTip(
            f"{len(numeric_fields)} numerical field(s) detected in the input grid."
        )

        if not numeric_fields:
            self.statusBar().showMessage(
                "No numerical fields were detected in the input grid. "
                "Check the source field types.",
                15000,
            )
            return

        self.statusBar().showMessage(
            f"Loaded {len(numeric_fields)} numerical input field(s).",
            8000,
        )

        for row, field in enumerate(numeric_fields):
            use_item = QTableWidgetItem()
            use_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            previous_category = previous.get(field.name())
            use_item.setCheckState(Qt.Checked if previous_category else Qt.Unchecked)
            self.variable_table.setItem(row, 0, use_item)

            name_item = QTableWidgetItem(field.name())
            name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.variable_table.setItem(row, 1, name_item)

            type_item = QTableWidgetItem(field.typeName() or str(field.type()))
            type_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.variable_table.setItem(row, 2, type_item)

            category_combo = QComboBox()
            category_combo.addItems(["Intensive", "Extensive"])
            if previous_category:
                category_combo.setCurrentText(previous_category)
            self.variable_table.setCellWidget(row, 3, category_combo)

    def _selected_variable_categories(self) -> Dict[str, str]:
        selected: Dict[str, str] = {}
        for row in range(self.variable_table.rowCount()):
            use_item = self.variable_table.item(row, 0)
            name_item = self.variable_table.item(row, 1)
            combo = self.variable_table.cellWidget(row, 3)
            if (
                use_item is not None
                and name_item is not None
                and use_item.checkState() == Qt.Checked
                and isinstance(combo, QComboBox)
            ):
                selected[name_item.text()] = combo.currentText()
        return selected

    def _set_all_variables(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.variable_table.rowCount()):
            item = self.variable_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _target_variable_rows(self) -> List[int]:
        selected_rows = sorted(
            {index.row() for index in self.variable_table.selectionModel().selectedRows()}
        )
        if selected_rows:
            return selected_rows

        checked_rows: List[int] = []
        for row in range(self.variable_table.rowCount()):
            item = self.variable_table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                checked_rows.append(row)
        return checked_rows

    def _set_selected_category(self, category: str) -> None:
        rows = self._target_variable_rows()
        if not rows:
            self.statusBar().showMessage(
                "Highlight one or more rows, or check variables before assigning a category.",
                10000,
            )
            return

        changed = 0
        for row in rows:
            combo = self.variable_table.cellWidget(row, 3)
            if isinstance(combo, QComboBox):
                combo.setCurrentText(category)
                changed += 1

        self.variable_table.viewport().update()
        self.statusBar().showMessage(
            f"Set {changed:,} variable(s) to {category}.",
            8000,
        )

    def _all_variable_states(self) -> List[Tuple[str, str, bool, str]]:
        states: List[Tuple[str, str, bool, str]] = []
        for row in range(self.variable_table.rowCount()):
            use_item = self.variable_table.item(row, 0)
            name_item = self.variable_table.item(row, 1)
            type_item = self.variable_table.item(row, 2)
            combo = self.variable_table.cellWidget(row, 3)
            if (
                use_item is None
                or name_item is None
                or type_item is None
                or not isinstance(combo, QComboBox)
            ):
                continue
            states.append(
                (
                    name_item.text(),
                    type_item.text(),
                    use_item.checkState() == Qt.Checked,
                    combo.currentText(),
                )
            )
        return states

    def _apply_variable_states(
        self,
        states: Dict[str, Tuple[bool, str]],
    ) -> None:
        checked_count = 0
        for row in range(self.variable_table.rowCount()):
            use_item = self.variable_table.item(row, 0)
            name_item = self.variable_table.item(row, 1)
            combo = self.variable_table.cellWidget(row, 3)
            if use_item is None or name_item is None or not isinstance(combo, QComboBox):
                continue
            state = states.get(name_item.text())
            if state is None:
                continue
            checked, category = state
            use_item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            combo.setCurrentText(category)
            if checked:
                checked_count += 1

        self.variable_table.viewport().update()
        self.statusBar().showMessage(
            f"Variable selection updated: {checked_count:,} variable(s) selected.",
            10000,
        )
        self._update_run_state()

    def _open_variable_selector(self) -> None:
        states = self._all_variable_states()
        if not states:
            QMessageBox.information(
                self,
                "No numerical variables",
                "Load an input grid containing numerical fields before opening the "
                "larger variable selector.",
            )
            return

        dialog = VariableSelectionDialog(states, self)
        if dialog.exec_() == QDialog.Accepted:
            self._apply_variable_states(dialog.variable_states())

    def _weighting_strategy_changed(self, _index: int) -> None:
        is_alternative = self.weighting_combo.currentText() == WEIGHT_ALTERNATIVE
        self.configure_weight_button.setEnabled(is_alternative)
        if not is_alternative:
            self.weighting_summary.setText(
                "Area based: intensive targets use a normalized area-weighted mean; "
                "extensive targets use the covered-share rule."
            )
            self.layers_created_label.setText("grid_intersections and reprojected_output")
            self.run_button.setText("Run grid transfer")
            self._update_run_state()
            return

        if self.input_selector.layer is None or self.output_selector.layer is None:
            QMessageBox.information(
                self,
                "Select grids first",
                "Select both input and output grids before configuring alternative weighting.",
            )
            self.weighting_combo.blockSignals(True)
            self.weighting_combo.setCurrentText(WEIGHT_AREA)
            self.weighting_combo.blockSignals(False)
            self.configure_weight_button.setEnabled(False)
            self._update_run_state()
            return

        if not self._configure_alternative_weighting():
            self.weighting_combo.blockSignals(True)
            self.weighting_combo.setCurrentText(WEIGHT_AREA)
            self.weighting_combo.blockSignals(False)
            self.configure_weight_button.setEnabled(False)
            self.weighting_summary.setText(
                "Area based: intensive targets use a normalized area-weighted mean; "
                "extensive targets use the covered-share rule."
            )
            self.layers_created_label.setText("grid_intersections and reprojected_output")
            self.run_button.setText("Run grid transfer")
        self._update_run_state()

    def _configure_alternative_weighting(self) -> bool:
        input_layer = self.input_selector.layer
        output_layer = self.output_selector.layer
        if input_layer is None or output_layer is None:
            return False

        selected_categories = self._selected_variable_categories().values()
        has_extensive_targets = any(category == "Extensive" for category in selected_categories)
        selected_categories = self._selected_variable_categories().values()
        has_intensive_targets = any(category == "Intensive" for category in selected_categories)
        dialog = AlternativeWeightDialog(
            input_layer,
            output_layer,
            has_extensive_targets,
            has_intensive_targets,
            self.alternative_weight_config,
            self,
        )
        if dialog.exec_() != QDialog.Accepted:
            return False

        config = dialog.configuration()
        if config is None:
            return False
        self.alternative_weight_config = config

        if config.template_only:
            self.weighting_summary.setText(
                f"Alternative extensive weight, {config.availability}. Preparation mode: "
                f"create grid_intersections with blank field '{config.template_weight_field}'."
            )
            self.layers_created_label.setText("grid_intersections only, weight preparation template")
            self.run_button.setText("Create intersection weight template")
        else:
            selected_parts = [
                f"type={config.variable_type}",
                f"source={config.availability}",
            ]
            if config.input_field:
                selected_parts.append(f"input field={config.input_field}")
            if config.output_field:
                selected_parts.append(f"output field={config.output_field}")
            if config.intersection_weight_field:
                selected_parts.append(f"intersection field={config.intersection_weight_field}")
            self.weighting_summary.setText("Alternative weighting: " + "; ".join(selected_parts))
            self.layers_created_label.setText("grid_intersections and reprojected_output")
            self.run_button.setText("Run grid transfer")
        self._update_run_state()
        return True

    def _browse_destination(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Select destination GeoPackage",
            self.destination_edit.text() or "grid_transfer.gpkg",
            "GeoPackage (*.gpkg)",
        )
        if path:
            if not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            self.destination_edit.setText(path)

    def _build_configuration(self) -> ProcessingConfiguration:
        input_layer = self.input_selector.layer
        output_layer = self.output_selector.layer
        if input_layer is None or output_layer is None:
            raise ValueError("Both input and output grids must be selected.")

        variables = tuple(self._selected_variable_categories().items())
        if not variables:
            raise ValueError("Select at least one numerical variable to transfer.")

        destination = self.destination_edit.text().strip()
        if not destination:
            raise ValueError("Select a destination GeoPackage.")
        if not destination.lower().endswith(".gpkg"):
            destination += ".gpkg"
            self.destination_edit.setText(destination)

        input_uid = self.input_selector.selected_uid_field()
        output_uid = self.output_selector.selected_uid_field()
        if not input_uid or not output_uid:
            raise ValueError("Select UID fields for both grids.")

        intersection_uid_field = self.intersection_uid_edit.text().strip()
        if not intersection_uid_field:
            raise ValueError("Define the field name for the combined intersection UID.")
        if not is_valid_field_name(intersection_uid_field):
            raise ValueError(
                "The intersection UID field name must contain only letters, digits, and "
                "underscores, and must not start with a digit."
            )

        target_crs_text = self.target_crs_edit.text().strip() or "EPSG:32632"
        target_crs = QgsCoordinateReferenceSystem()
        if not target_crs.createFromUserInput(target_crs_text) or not target_crs.isValid():
            raise ValueError(
                "Define a valid target CRS, for example EPSG:32632 or another valid CRS identifier."
            )
        self.target_crs_edit.setText(target_crs.authid() or target_crs_text)

        intersection_mode = self.intersection_mode_combo.currentText()
        if intersection_mode == INTERSECTION_EXISTING:
            layer = self.existing_intersection_layer
            if layer is None or not layer.isValid():
                raise ValueError("Load a valid existing polygon intersection layer.")
            source_path = self.existing_intersection_path_edit.text().strip()
            source_intersection_uid = str(
                self.existing_intersection_uid_combo.currentData() or ""
            )
            source_input_uid = str(
                self.existing_intersection_input_uid_combo.currentData() or ""
            )
            source_output_uid = str(
                self.existing_intersection_output_uid_combo.currentData() or ""
            )
            if not source_path or not source_intersection_uid or not source_input_uid or not source_output_uid:
                raise ValueError(
                    "Identify the intersection UID, input UID, and output UID fields in the "
                    "existing intersection layer."
                )
            intersection_source = IntersectionSourceConfiguration(
                mode=INTERSECTION_EXISTING,
                source_path=source_path,
                intersection_uid_field=source_intersection_uid,
                input_uid_field=source_input_uid,
                output_uid_field=source_output_uid,
                area_field=(
                    str(self.existing_intersection_area_combo.currentData())
                    if self.existing_intersection_area_combo.currentData()
                    else None
                ),
            )
        else:
            intersection_source = IntersectionSourceConfiguration(mode=INTERSECTION_COMPUTE)

        output_field_prefix = self.output_prefix_edit.text().strip().rstrip("_")
        if output_field_prefix and not is_valid_field_name(output_field_prefix):
            raise ValueError(
                "The reprojected field prefix must contain only letters, digits, and "
                "underscores, and must not start with a digit. It may also be left blank."
            )

        strategy = self.weighting_combo.currentText()
        if strategy == WEIGHT_ALTERNATIVE and self.alternative_weight_config is None:
            raise ValueError("Configure the alternative weighting before processing.")

        return ProcessingConfiguration(
            input_path=input_layer.source(),
            output_path=output_layer.source(),
            input_uid_field=input_uid,
            output_uid_field=output_uid,
            intersection_uid_field=intersection_uid_field,
            input_area_field=self.input_selector.selected_area_field(),
            output_area_field=self.output_selector.selected_area_field(),
            variables=variables,
            output_field_prefix=output_field_prefix,
            destination_gpkg=destination,
            target_crs=target_crs.authid() or target_crs_text,
            intersection_source=intersection_source,
            weighting_strategy=strategy,
            alternative_weight=self.alternative_weight_config,
        )

    def _run(self) -> None:
        try:
            configuration = self._build_configuration()
        except Exception as exc:
            QMessageBox.warning(self, "Configuration error", str(exc))
            return

        destination = Path(configuration.destination_gpkg)
        if destination.exists():
            answer = QMessageBox.question(
                self,
                "Overwrite output",
                f"The file already exists and will be replaced:\n{destination}\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        self.run_button.setEnabled(False)
        self.log.clear()
        self.progress.setValue(0)
        self._log(f"Starting transfer with weighting strategy: {configuration.weighting_strategy}.")
        self._log(
            "Variables to reproject: "
            + ", ".join(
                f"{field_name} ({category})"
                for field_name, category in configuration.variables
            )
        )
        self._log(
            "Weight used: "
            + describe_weighting_configuration(
                configuration.weighting_strategy,
                configuration.alternative_weight,
            )
        )
        self._log(
            f"Reprojected field prefix: '{configuration.output_field_prefix}'"
            if configuration.output_field_prefix
            else "Reprojected field prefix: none"
        )

        try:
            processor = GridTransferProcessor(configuration, self._log, self._set_progress)
            summary = processor.run()
            self.progress.setValue(100)
            self._log("Processing completed successfully.")
            self._log(
                f"Output polygons inspected: {summary['output_features']:,}; "
                f"intersection relationships: {summary['intersections']:,}; "
                f"uncovered output polygons: {summary['uncovered_outputs']:,}."
            )
            self._log(
                f"Warning log: {summary['warning_log']} "
                f"({summary['warning_count']:,} warning entr"
                f"{'y' if summary['warning_count'] == 1 else 'ies'})."
            )

            if summary["template_only"]:
                message = (
                    "Intersection weight template created.\n\n"
                    f"GeoPackage: {configuration.destination_gpkg}\n"
                    f"Warning log: {summary['warning_log']}\n"
                    "Layer: grid_intersections\n\n"
                    "Populate the blank fragment-weight field, then rerun with Alternative "
                    "weighting, Extensive, Existing intersection layer."
                )
            else:
                message = (
                    "Grid transfer completed.\n\n"
                    f"GeoPackage: {configuration.destination_gpkg}\n"
                    f"Warning log: {summary['warning_log']}\n"
                    "Layers: grid_intersections, reprojected_output"
                )
            QMessageBox.information(self, "Completed", message)
        except Exception as exc:
            self._log(traceback.format_exc())
            QMessageBox.critical(self, "Processing error", str(exc))
        finally:
            self._update_run_state()

    def _log(self, message: str) -> None:
        self.log.append(message)
        QgsApplication.processEvents()

    def _set_progress(self, value: int) -> None:
        self.progress.setValue(max(0, min(100, value)))
        QgsApplication.processEvents()


class GridTransferProcessor:
    def __init__(self, config: ProcessingConfiguration, log_callback, progress_callback) -> None:
        self.config = config
        self.log = log_callback
        self.progress = progress_callback
        self.context = QgsProject.instance().transformContext()

        self.input_layer = QgsVectorLayer(config.input_path, "input_grid", "ogr")
        self.output_layer = QgsVectorLayer(config.output_path, "output_grid", "ogr")
        if not self.input_layer.isValid() or not self.output_layer.isValid():
            raise RuntimeError("The input or output grid could not be loaded.")

        if not self.input_layer.crs().isValid():
            raise RuntimeError("The input grid has no valid CRS.")
        if not self.output_layer.crs().isValid():
            raise RuntimeError("The output grid has no valid CRS.")

        self.calculation_crs = QgsCoordinateReferenceSystem()
        if (
            not self.calculation_crs.createFromUserInput(config.target_crs)
            or not self.calculation_crs.isValid()
        ):
            raise RuntimeError(f"The configured target CRS is invalid: {config.target_crs}")

        self.distance_area = QgsDistanceArea()
        self.distance_area.setSourceCrs(self.calculation_crs, self.context)
        ellipsoid = self.calculation_crs.ellipsoidAcronym()
        self.distance_area.setEllipsoid(ellipsoid if ellipsoid else "WGS84")

        self._validate_intersection_uid_field_name()
        self._intersection_uid_pairs: Dict[str, Tuple[str, str]] = {}
        self.external_weight_layer: Optional[QgsVectorLayer] = None
        self.external_weights: Dict[str, Tuple[float, str]] = {}
        self.used_external_weight_keys: set = set()
        self.input_weight_totals: Dict[str, float] = {}

        # Warnings are collected during processing and written to a log file
        # beside the output GeoPackage. Keys prevent repeated messages when the
        # same condition is encountered during both the preliminary and final pass.
        self.warning_messages: List[str] = []
        self._warning_keys: set = set()

        output_used = set(self.output_layer.fields().names())
        self.coverage_field = unique_field_name("cov_ratio", output_used)
        self.input_count_field = unique_field_name("n_inputs", output_used)
        self.variable_specs = self._make_variable_specs(output_used)

        self.intersection_used_names = {
            self.config.intersection_uid_field,
            "input_uid",
            "output_uid",
            "if_area_m2",
            "ip_area_m2",
            "op_area_m2",
            "ra_input",
            "ra_output",
        }
        for spec in self.variable_specs:
            self.intersection_used_names.add(spec.intersection_source_field)
            if spec.intersection_value_field:
                self.intersection_used_names.add(spec.intersection_value_field)

        self.weight_source_input_field = unique_field_name(
            "weight_src_input", self.intersection_used_names
        )
        self.weight_source_output_field = unique_field_name(
            "weight_src_output", self.intersection_used_names
        )
        self.weight_source_intersection_field = unique_field_name(
            "weight_src_intersection", self.intersection_used_names
        )
        self.fragment_weight_field = unique_field_name(
            "weight_fragment", self.intersection_used_names
        )

    @property
    def alternative(self) -> Optional[AlternativeWeightConfiguration]:
        return self.config.alternative_weight

    @property
    def template_only(self) -> bool:
        return bool(
            self.config.weighting_strategy == WEIGHT_ALTERNATIVE
            and self.alternative is not None
            and self.alternative.template_only
        )

    def run(self) -> Dict[str, Any]:
        self._intersection_uid_pairs.clear()
        self._validate_fields()
        self._validate_uid(self.input_layer, self.config.input_uid_field, "input")
        self._validate_uid(self.output_layer, self.config.output_uid_field, "output")
        self._prepare_external_weights_if_needed()

        destination = Path(self.config.destination_gpkg)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()

        if self.config.intersection_source.mode == INTERSECTION_EXISTING:
            return self._run_with_existing_intersections(destination)

        self.log("Preparing input geometries and spatial index.")
        input_records, spatial_index = self._prepare_input_records()
        self.progress(15)

        if self._requires_alternative_input_totals():
            self.log(
                "Calculating alternative-weight totals for input polygons. "
                "This preliminary pass is required for extensive target variables."
            )
            self.input_weight_totals = self._precompute_input_weight_totals(
                input_records,
                spatial_index,
            )
            self.progress(30)

        intersection_fields = self._intersection_fields()
        intersection_writer = self._create_writer(
            destination=str(destination),
            layer_name="grid_intersections",
            fields=intersection_fields,
            geometry_type=QgsWkbTypes.MultiPolygon,
            crs=self.calculation_crs,
            overwrite_file=True,
        )

        output_writer: Optional[QgsVectorFileWriter] = None
        temporary_output = self._make_temporary_output_path(destination)
        output_fields: Optional[QgsFields] = None

        if not self.template_only:
            output_fields = self._output_fields()
            self._remove_temporary_output(temporary_output, record_warning=False)
            output_writer = self._create_writer(
                destination=str(temporary_output),
                layer_name="reprojected_output",
                fields=output_fields,
                geometry_type=self.output_layer.wkbType(),
                crs=self.calculation_crs,
                overwrite_file=True,
            )

        output_count = int(self.output_layer.featureCount())
        intersections_count = 0
        uncovered_count = 0
        start_progress = 30 if self._requires_alternative_input_totals() else 15

        try:
            for position, output_feature in enumerate(self.output_layer.getFeatures(), start=1):
                result_feature, created = self._process_output_feature(
                    output_feature,
                    input_records,
                    spatial_index,
                    intersection_fields,
                    intersection_writer,
                    output_fields,
                )
                if not self.template_only:
                    assert output_writer is not None and result_feature is not None
                    if not output_writer.addFeature(result_feature, QgsFeatureSink.FastInsert):
                        raise RuntimeError("Could not write an output feature.")

                intersections_count += created
                if created == 0:
                    uncovered_count += 1

                if output_count > 0:
                    self.progress(start_progress + int(60 * position / output_count))

            del intersection_writer
            intersection_writer = None

            if output_writer is not None:
                del output_writer
                output_writer = None
                gc.collect()
                QgsApplication.processEvents()
                self._append_temporary_output(destination, temporary_output)

            if self.external_weights:
                unused = len(set(self.external_weights) - self.used_external_weight_keys)
                if unused:
                    self.log(
                        f"Warning: {unused:,} external intersection-weight records were not "
                        "matched to the intersections generated in this session."
                    )
            self.progress(95)
        finally:
            if intersection_writer is not None:
                del intersection_writer
            if output_writer is not None:
                del output_writer
            self._remove_temporary_output(temporary_output, record_warning=True)

        summary = {
            "output_features": output_count,
            "intersections": intersections_count,
            "uncovered_outputs": uncovered_count,
            "template_only": self.template_only,
        }
        warning_log = self._write_warning_log(destination, summary)
        summary["warning_count"] = len(self.warning_messages)
        summary["warning_log"] = str(warning_log)
        return summary

    def _run_with_existing_intersections(self, destination: Path) -> Dict[str, Any]:
        """Run the transfer using geometries and relationships from an uploaded layer."""
        source_layer = self._load_existing_intersection_processing_layer()
        self.log(
            "Using the uploaded intersection layer directly. No input/output geometry "
            "intersection will be recomputed."
        )

        self.log("Preparing input geometries and attributes in the target CRS.")
        input_records, _unused_index = self._prepare_input_records()
        input_by_uid = {str(record.uid): record for record in input_records.values()}
        output_records = self._prepare_output_records()
        output_by_uid = {str(record.uid): record for record in output_records}
        self.progress(15)

        if self._requires_alternative_input_totals():
            self.log(
                "Calculating alternative-weight totals from the uploaded intersection layer."
            )
            self.input_weight_totals = self._precompute_existing_input_weight_totals(
                source_layer, input_by_uid, output_by_uid
            )
            self.progress(30)

        intersection_fields = self._intersection_fields()
        intersection_writer = self._create_writer(
            destination=str(destination),
            layer_name="grid_intersections",
            fields=intersection_fields,
            geometry_type=QgsWkbTypes.MultiPolygon,
            crs=self.calculation_crs,
            overwrite_file=True,
        )

        output_fields: Optional[QgsFields] = None
        output_writer: Optional[QgsVectorFileWriter] = None
        temporary_output = self._make_temporary_output_path(destination)
        if not self.template_only:
            output_fields = self._output_fields()
            self._remove_temporary_output(temporary_output, record_warning=False)
            output_writer = self._create_writer(
                destination=str(temporary_output),
                layer_name="reprojected_output",
                fields=output_fields,
                geometry_type=self.output_layer.wkbType(),
                crs=self.calculation_crs,
                overwrite_file=True,
            )

        output_stats: Dict[str, Dict[str, Any]] = {}
        for output_key in output_by_uid:
            output_stats[output_key] = {
                "area": 0.0,
                "count": 0,
                "variables": self._empty_variable_accumulators(),
            }

        intersections_count = 0
        source_count = int(source_layer.featureCount())
        self._intersection_uid_pairs.clear()

        try:
            for position, source_feature in enumerate(source_layer.getFeatures(), start=1):
                (
                    source_intersection_uid,
                    input_key,
                    output_key,
                    intersection_geometry,
                    intersection_area,
                ) = self._read_existing_intersection_feature(source_feature, source_layer)

                record = input_by_uid.get(input_key)
                if record is None:
                    raise ValueError(
                        f"Intersection '{source_intersection_uid}' references input UID "
                        f"'{input_key}', which is not present in the selected input grid."
                    )
                output_record = output_by_uid.get(output_key)
                if output_record is None:
                    raise ValueError(
                        f"Intersection '{source_intersection_uid}' references output UID "
                        f"'{output_key}', which is not present in the selected output grid."
                    )

                intersection_uid = self._register_supplied_intersection_uid(
                    source_intersection_uid, output_record.uid, record.uid
                )
                ra_input = intersection_area / record.area_m2
                ra_output = intersection_area / output_record.area_m2

                fragment_weight = None
                weight_src_input = None
                weight_src_output = None
                weight_src_intersection = None
                if self.config.weighting_strategy == WEIGHT_ALTERNATIVE:
                    if self.template_only:
                        weight_src_input = record.weight_value
                        weight_src_output = output_record.weight_value
                    else:
                        (
                            fragment_weight,
                            weight_src_input,
                            weight_src_output,
                            weight_src_intersection,
                        ) = self._alternative_fragment_weight(
                            record,
                            output_record.weight_value,
                            intersection_uid,
                            intersection_area,
                        )

                intersection_feature = QgsFeature(intersection_fields)
                intersection_feature.setGeometry(force_multipolygon(intersection_geometry))
                intersection_feature[self.config.intersection_uid_field] = intersection_uid
                intersection_feature["input_uid"] = record.uid
                intersection_feature["output_uid"] = output_record.uid
                intersection_feature["if_area_m2"] = intersection_area
                intersection_feature["ip_area_m2"] = record.area_m2
                intersection_feature["op_area_m2"] = output_record.area_m2
                intersection_feature["ra_input"] = ra_input
                intersection_feature["ra_output"] = ra_output

                if self.config.weighting_strategy == WEIGHT_ALTERNATIVE:
                    self._set_optional_field(
                        intersection_feature,
                        intersection_fields,
                        self.weight_source_input_field,
                        weight_src_input,
                    )
                    self._set_optional_field(
                        intersection_feature,
                        intersection_fields,
                        self.weight_source_output_field,
                        weight_src_output,
                    )
                    self._set_optional_field(
                        intersection_feature,
                        intersection_fields,
                        self.weight_source_intersection_field,
                        weight_src_intersection,
                    )
                    if self.template_only:
                        assert self.alternative is not None
                        intersection_feature[self.alternative.template_weight_field] = NULL
                    else:
                        self._set_optional_field(
                            intersection_feature,
                            intersection_fields,
                            self.fragment_weight_field,
                            fragment_weight,
                        )

                stats = output_stats[output_key]
                stats["area"] += intersection_area
                stats["count"] += 1
                accumulators = stats["variables"]

                for spec in self.variable_specs:
                    value = record.values[spec.source_field]
                    intersection_feature[spec.intersection_source_field] = value
                    if value is None or self.template_only:
                        if spec.intersection_value_field:
                            intersection_feature[spec.intersection_value_field] = NULL
                        continue

                    accumulator = accumulators[spec.source_field]
                    accumulator["count"] += 1.0
                    if self.config.weighting_strategy == WEIGHT_AREA:
                        if spec.category == "Intensive":
                            accumulator["numerator"] += value * ra_output
                            accumulator["denominator"] += ra_output
                        else:
                            fragment_value = value * ra_input
                            accumulator["sum"] += fragment_value
                            assert spec.intersection_value_field is not None
                            intersection_feature[spec.intersection_value_field] = fragment_value
                    else:
                        assert fragment_weight is not None
                        if spec.category == "Intensive":
                            accumulator["numerator"] += value * fragment_weight
                            accumulator["denominator"] += fragment_weight
                        else:
                            denominator = self.input_weight_totals.get(input_key, 0.0)
                            if denominator <= 0.0:
                                fragment_value = 0.0
                                accumulator["forced_zero_count"] += 1.0
                                self._add_warning(
                                    "ZERO_INPUT_WEIGHT_TOTAL",
                                    f"Input polygon UID '{record.uid}' has total extensive "
                                    "alternative weight 0. Its contribution to output polygon "
                                    f"UID '{output_record.uid}' for variable '{spec.source_field}' "
                                    "was set to 0.",
                                    key=(
                                        f"allocation:{record.uid}:{output_record.uid}:"
                                        f"{spec.source_field}"
                                    ),
                                )
                            else:
                                fragment_value = value * fragment_weight / denominator
                            accumulator["sum"] += fragment_value
                            assert spec.intersection_value_field is not None
                            intersection_feature[spec.intersection_value_field] = fragment_value

                if not intersection_writer.addFeature(
                    intersection_feature, QgsFeatureSink.FastInsert
                ):
                    raise RuntimeError("Could not write an uploaded intersection feature.")
                intersections_count += 1

                if source_count > 0 and position % 100 == 0:
                    start = 30 if self._requires_alternative_input_totals() else 15
                    self.progress(start + int(50 * position / source_count))

            del intersection_writer
            intersection_writer = None

            uncovered_count = 0
            if not self.template_only:
                assert output_writer is not None and output_fields is not None
                output_count = len(output_records)
                for position, output_record in enumerate(output_records, start=1):
                    output_key = str(output_record.uid)
                    stats = output_stats[output_key]
                    result = QgsFeature(output_fields)
                    result.setGeometry(output_record.geometry)
                    for field_name in self.output_layer.fields().names():
                        result[field_name] = output_record.feature[field_name]

                    relationship_count = int(stats["count"])
                    if relationship_count == 0:
                        uncovered_count += 1
                    result[self.coverage_field] = (
                        float(stats["area"]) / output_record.area_m2
                        if relationship_count > 0
                        else 0.0
                    )
                    result[self.input_count_field] = relationship_count

                    accumulators = stats["variables"]
                    for spec in self.variable_specs:
                        accumulator = accumulators[spec.source_field]
                        if accumulator["count"] == 0:
                            result[spec.output_field] = NULL
                        elif spec.category == "Intensive":
                            denominator = accumulator["denominator"]
                            if denominator > 0:
                                result[spec.output_field] = (
                                    accumulator["numerator"] / denominator
                                )
                            elif self.config.weighting_strategy == WEIGHT_ALTERNATIVE:
                                result[spec.output_field] = 0.0
                                self._add_warning(
                                    "ZERO_OUTPUT_WEIGHT_DENOMINATOR",
                                    f"Output polygon UID '{output_record.uid}' has total "
                                    f"alternative weight 0 for intensive variable "
                                    f"'{spec.source_field}'. The reprojected value was set to 0.",
                                    key=(
                                        f"output-denominator:{output_record.uid}:"
                                        f"{spec.source_field}"
                                    ),
                                )
                            else:
                                result[spec.output_field] = NULL
                        else:
                            result[spec.output_field] = accumulator["sum"]
                            if (
                                self.config.weighting_strategy == WEIGHT_ALTERNATIVE
                                and accumulator["sum"] == 0.0
                                and accumulator["forced_zero_count"] > 0.0
                            ):
                                self._add_warning(
                                    "ZERO_OUTPUT_VALUE_FROM_ZERO_WEIGHTS",
                                    f"Output polygon UID '{output_record.uid}' received a "
                                    f"reprojected value of 0 for extensive variable "
                                    f"'{spec.source_field}' because one or more contributing "
                                    "input polygons had total alternative weight 0.",
                                    key=(
                                        f"output-zero:{output_record.uid}:"
                                        f"{spec.source_field}"
                                    ),
                                )

                    if not output_writer.addFeature(result, QgsFeatureSink.FastInsert):
                        raise RuntimeError("Could not write a reprojected output feature.")
                    if output_count > 0 and position % 100 == 0:
                        self.progress(80 + int(10 * position / output_count))

                del output_writer
                output_writer = None
                gc.collect()
                QgsApplication.processEvents()
                self._append_temporary_output(destination, temporary_output)
            else:
                uncovered_count = sum(
                    1 for stats in output_stats.values() if int(stats["count"]) == 0
                )

            if self.external_weights:
                unused = len(set(self.external_weights) - self.used_external_weight_keys)
                if unused:
                    self.log(
                        f"Warning: {unused:,} external intersection-weight records were not "
                        "matched to the uploaded intersection relationships."
                    )
            self.progress(95)
        finally:
            if intersection_writer is not None:
                del intersection_writer
            if output_writer is not None:
                del output_writer
            self._remove_temporary_output(temporary_output, record_warning=True)

        summary = {
            "output_features": len(output_records),
            "intersections": intersections_count,
            "uncovered_outputs": uncovered_count,
            "template_only": self.template_only,
        }
        warning_log = self._write_warning_log(destination, summary)
        summary["warning_count"] = len(self.warning_messages)
        summary["warning_log"] = str(warning_log)
        return summary

    def _load_existing_intersection_processing_layer(self) -> QgsVectorLayer:
        config = self.config.intersection_source
        if config.mode != INTERSECTION_EXISTING or not config.source_path:
            raise RuntimeError("Existing intersection mode is not configured correctly.")
        layer = QgsVectorLayer(config.source_path, "existing_intersections", "ogr")
        if not layer.isValid():
            raise ValueError("The configured existing intersection layer cannot be loaded.")
        if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
            raise ValueError("The existing intersection layer must contain polygon geometries.")
        if not layer.crs().isValid():
            raise ValueError("The existing intersection layer has no valid CRS.")

        required = {
            config.intersection_uid_field,
            config.input_uid_field,
            config.output_uid_field,
        }
        if config.area_field:
            required.add(config.area_field)
        names = set(layer.fields().names())
        missing = sorted(name for name in required if name and name not in names)
        if missing:
            raise ValueError(
                "The existing intersection layer is missing fields: " + ", ".join(missing)
            )
        return layer

    def _prepare_output_records(self) -> List[OutputRecord]:
        records: List[OutputRecord] = []
        for feature in self.output_layer.getFeatures():
            geometry = self._geometry_in_calculation_crs(
                feature.geometry(), self.output_layer.crs(), "output"
            )
            area = self._feature_area(
                feature, geometry, self.config.output_area_field, "output"
            )
            records.append(
                OutputRecord(
                    uid=feature[self.config.output_uid_field],
                    feature=QgsFeature(feature),
                    geometry=geometry,
                    area_m2=area,
                    weight_value=self._output_weight_value(feature),
                )
            )
        return records

    def _read_existing_intersection_feature(
        self, feature: QgsFeature, layer: QgsVectorLayer
    ) -> Tuple[str, str, str, QgsGeometry, float]:
        config = self.config.intersection_source
        assert config.intersection_uid_field
        assert config.input_uid_field
        assert config.output_uid_field

        uid_value = feature[config.intersection_uid_field]
        input_uid_value = feature[config.input_uid_field]
        output_uid_value = feature[config.output_uid_field]
        if is_null(uid_value) or is_null(input_uid_value) or is_null(output_uid_value):
            raise ValueError(
                f"Existing intersection feature {feature.id()} has a null intersection, "
                "input, or output UID."
            )

        geometry = self._geometry_in_calculation_crs(
            feature.geometry(), layer.crs(), "intersection"
        )
        if config.area_field:
            area = to_finite_float(feature[config.area_field])
            if area is None or area <= 0:
                raise ValueError(
                    f"Existing intersection feature {feature.id()} has an invalid area in "
                    f"field '{config.area_field}'. Values must be positive square metres."
                )
        else:
            area = float(self.distance_area.measureArea(geometry))
            if not math.isfinite(area) or area <= 0:
                raise ValueError(
                    f"Existing intersection feature {feature.id()} has an invalid geometry area."
                )
        return (
            str(uid_value),
            str(input_uid_value),
            str(output_uid_value),
            geometry,
            float(area),
        )

    def _precompute_existing_input_weight_totals(
        self,
        layer: QgsVectorLayer,
        input_by_uid: Dict[str, InputRecord],
        output_by_uid: Dict[str, OutputRecord],
    ) -> Dict[str, float]:
        totals: Dict[str, float] = {}
        contributors: set = set()
        feature_count = int(layer.featureCount())
        for position, feature in enumerate(layer.getFeatures(), start=1):
            uid, input_key, output_key, _geometry, area = self._read_existing_intersection_feature(
                feature, layer
            )
            record = input_by_uid.get(input_key)
            output_record = output_by_uid.get(output_key)
            if record is None or output_record is None:
                raise ValueError(
                    f"Intersection '{uid}' references an input or output UID that is not "
                    "present in the selected grids."
                )
            weight, _, _, _ = self._alternative_fragment_weight(
                record, output_record.weight_value, uid, area
            )
            contributors.add(input_key)
            totals[input_key] = totals.get(input_key, 0.0) + weight
            if feature_count > 0 and position % 100 == 0:
                self.progress(15 + int(15 * position / feature_count))

        zero_totals = sorted(key for key in contributors if totals.get(key, 0.0) <= 0.0)
        for input_key in zero_totals:
            self._add_warning(
                "ZERO_INPUT_WEIGHT_TOTAL",
                f"Input polygon UID '{input_key}' has total extensive alternative weight 0 "
                "across its intersection features. Its extensive target contributions are "
                "set to 0.",
                key=f"input-total:{input_key}",
            )
        return totals

    def _empty_variable_accumulators(self) -> Dict[str, Dict[str, float]]:
        return {
            spec.source_field: {
                "numerator": 0.0,
                "denominator": 0.0,
                "sum": 0.0,
                "count": 0.0,
                "forced_zero_count": 0.0,
            }
            for spec in self.variable_specs
        }

    def _register_supplied_intersection_uid(
        self, supplied_uid: str, output_uid: Any, input_uid: Any
    ) -> str:
        key = str(supplied_uid)
        pair = (str(output_uid), str(input_uid))
        existing_pair = self._intersection_uid_pairs.get(key)
        if existing_pair is not None:
            raise ValueError(
                f"The uploaded intersection UID field is not unique. Duplicate key: '{key}'."
            )
        self._intersection_uid_pairs[key] = pair
        return key


    def _append_temporary_output(self, destination: Path, temporary_output: Path) -> None:
        temporary_layer = QgsVectorLayer(
            f"{temporary_output}|layername=reprojected_output",
            "reprojected_output",
            "ogr",
        )
        if not temporary_layer.isValid():
            raise RuntimeError("Could not reopen the temporary output layer.")

        try:
            save_options = QgsVectorFileWriter.SaveVectorOptions()
            save_options.driverName = "GPKG"
            save_options.layerName = "reprojected_output"
            save_options.fileEncoding = "UTF-8"
            save_options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
            error, error_message, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
                temporary_layer,
                str(destination),
                self.context,
                save_options,
            )
            if error != QgsVectorFileWriter.NoError:
                raise RuntimeError(
                    "Could not add the reprojected output layer to the GeoPackage: "
                    f"{error_message}"
                )
        finally:
            # On Windows, the OGR provider can keep the temporary GeoPackage open
            # until the underlying Qt/C++ object is explicitly destroyed.
            try:
                from qgis.PyQt import sip

                if not sip.isdeleted(temporary_layer):
                    sip.delete(temporary_layer)
            except Exception:
                # Falling back to normal Python garbage collection remains safe.
                pass
            temporary_layer = None
            gc.collect()
            QgsApplication.processEvents()

        # Cleanup is deliberately nonfatal. The requested output has already been
        # written successfully, so a delayed Windows file lock must not turn the
        # completed transfer into a processing failure.
        self._remove_temporary_output(temporary_output, record_warning=True)

    def _make_temporary_output_path(self, destination: Path) -> Path:
        """Return a unique temporary GeoPackage path for the current run."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return destination.with_name(
            f"{destination.stem}_temporary_output_{os.getpid()}_{stamp}.gpkg"
        )

    def _remove_temporary_output(
        self,
        temporary_output: Path,
        record_warning: bool,
        attempts: int = 12,
        delay_seconds: float = 0.15,
    ) -> bool:
        """Remove a temporary GeoPackage after releasing delayed Windows locks."""
        if not temporary_output.exists():
            return True

        last_error: Optional[OSError] = None
        for _attempt in range(attempts):
            try:
                temporary_output.unlink()
                self._remove_sqlite_sidecars(temporary_output)
                return True
            except FileNotFoundError:
                return True
            except PermissionError as exc:
                last_error = exc
                gc.collect()
                QgsApplication.processEvents()
                time.sleep(delay_seconds)
            except OSError as exc:
                last_error = exc
                break

        if record_warning:
            message = (
                f"The temporary GeoPackage '{temporary_output}' could not be deleted "
                "because it remained locked by the Windows QGIS/OGR process. The main "
                "output GeoPackage was written successfully. The temporary file can be "
                "deleted manually after the application is closed."
            )
            if last_error is not None:
                message += f" System message: {last_error}"
            self.log(f"Warning: {message}")
            self._add_warning(
                "TEMPORARY_FILE_LOCKED",
                message,
                key=f"temporary-file:{temporary_output}",
            )
        return False

    @staticmethod
    def _remove_sqlite_sidecars(database_path: Path) -> None:
        """Remove SQLite WAL and shared-memory sidecars when they remain."""
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(database_path) + suffix)
            if sidecar.exists():
                try:
                    sidecar.unlink()
                except OSError:
                    pass

    def _validate_intersection_uid_field_name(self) -> None:
        field_name = self.config.intersection_uid_field.strip()
        if not field_name:
            raise ValueError("The combined intersection UID field name cannot be empty.")
        reserved = {
            "input_uid",
            "output_uid",
            "if_area_m2",
            "ip_area_m2",
            "op_area_m2",
            "ra_input",
            "ra_output",
        }
        if field_name.lower() in {name.lower() for name in reserved}:
            raise ValueError(
                f"The intersection UID field name '{field_name}' conflicts with a reserved name."
            )

    def _validate_fields(self) -> None:
        input_names = set(self.input_layer.fields().names())
        output_names = set(self.output_layer.fields().names())

        required_input = {self.config.input_uid_field, *(n for n, _ in self.config.variables)}
        if self.config.input_area_field:
            required_input.add(self.config.input_area_field)
        if (
            self.config.weighting_strategy == WEIGHT_ALTERNATIVE
            and self.alternative is not None
            and self.alternative.input_field
        ):
            required_input.add(self.alternative.input_field)
        missing_input = sorted(required_input - input_names)
        if missing_input:
            raise ValueError(f"Missing input fields: {', '.join(missing_input)}")

        required_output = {self.config.output_uid_field}
        if self.config.output_area_field:
            required_output.add(self.config.output_area_field)
        if (
            self.config.weighting_strategy == WEIGHT_ALTERNATIVE
            and self.alternative is not None
            and self.alternative.output_field
        ):
            required_output.add(self.alternative.output_field)
        missing_output = sorted(required_output - output_names)
        if missing_output:
            raise ValueError(f"Missing output fields: {', '.join(missing_output)}")

    def _validate_uid(self, layer: QgsVectorLayer, field_name: str, label: str) -> None:
        seen = set()
        for feature in layer.getFeatures():
            value = feature[field_name]
            if is_null(value):
                raise ValueError(f"The {label} UID field contains null values: {field_name}")
            key = (type(value).__name__, str(value))
            if key in seen:
                raise ValueError(f"The {label} UID field is not unique: {field_name}")
            seen.add(key)

    def _make_variable_specs(self, output_used: set) -> List[VariableSpec]:
        intersection_used = {
            self.config.intersection_uid_field,
            "input_uid",
            "output_uid",
            "if_area_m2",
            "ip_area_m2",
            "op_area_m2",
            "ra_input",
            "ra_output",
        }
        specs: List[VariableSpec] = []
        for source_name, category in self.config.variables:
            output_base_name = (
                f"{self.config.output_field_prefix}_{source_name}"
                if self.config.output_field_prefix
                else source_name
            )
            output_name = unique_field_name(output_base_name, output_used)
            source_copy_name = unique_field_name(f"src_{source_name}", intersection_used)
            value_name = None
            if category == "Extensive":
                value_name = unique_field_name(f"vv_{source_name}", intersection_used)
            specs.append(
                VariableSpec(
                    source_field=source_name,
                    category=category,
                    output_field=output_name,
                    intersection_source_field=source_copy_name,
                    intersection_value_field=value_name,
                )
            )
        return specs

    def _prepare_external_weights_if_needed(self) -> None:
        if (
            self.config.weighting_strategy != WEIGHT_ALTERNATIVE
            or self.alternative is None
            or self.alternative.availability != SOURCE_INTERSECTION
        ):
            return

        alt = self.alternative
        assert alt.intersection_path
        assert alt.intersection_uid_field
        assert alt.intersection_input_uid_field
        assert alt.intersection_weight_field

        layer = QgsVectorLayer(alt.intersection_path, "intersection_weights", "ogr")
        if not layer.isValid():
            raise ValueError("The configured intersection weight layer cannot be loaded.")
        names = set(layer.fields().names())
        required = {
            alt.intersection_uid_field,
            alt.intersection_input_uid_field,
            alt.intersection_weight_field,
        }
        missing = sorted(required - names)
        if missing:
            raise ValueError(
                "The intersection weight layer is missing fields: " + ", ".join(missing)
            )

        mapping: Dict[str, Tuple[float, str]] = {}
        for feature in layer.getFeatures():
            key_value = feature[alt.intersection_uid_field]
            input_uid_value = feature[alt.intersection_input_uid_field]
            if is_null(key_value) or is_null(input_uid_value):
                raise ValueError(
                    "The external intersection UID or input UID field contains null values."
                )
            key = str(key_value)
            if key in mapping:
                raise ValueError(
                    f"The external intersection UID field is not unique. Duplicate: {key}"
                )
            raw_weight = feature[alt.intersection_weight_field]
            if is_null(raw_weight) and alt.variable_type == WEIGHT_EXTENSIVE:
                weight = 0.0
                self._add_warning(
                    "NULL_EXTENSIVE_WEIGHT",
                    f"Intersection '{key}' has a null extensive weighting value in field "
                    f"'{alt.intersection_weight_field}'. The fragment weight was set to 0.",
                    key=f"external:{key}",
                )
            else:
                weight = to_nonnegative_float(raw_weight)
                if weight is None:
                    raise ValueError(
                        f"Invalid, null, or negative external weight for intersection key '{key}'."
                    )
            mapping[key] = (weight, str(input_uid_value))

        self.external_weight_layer = layer
        self.external_weights = mapping
        self.log(f"Loaded {len(mapping):,} external intersection weights.")

    def _prepare_input_records(self) -> Tuple[Dict[int, InputRecord], QgsSpatialIndex]:
        transform = None
        if self.input_layer.crs() != self.calculation_crs:
            transform = QgsCoordinateTransform(
                self.input_layer.crs(),
                self.calculation_crs,
                QgsProject.instance(),
            )
            self.log(
                f"Input geometries will be transformed from {self.input_layer.crs().authid()} "
                f"to {self.calculation_crs.authid()} for intersection calculations."
            )

        records: Dict[int, InputRecord] = {}
        index = QgsSpatialIndex()
        input_count = int(self.input_layer.featureCount())
        alt_input_field = self.alternative.input_field if self.alternative else None

        for position, feature in enumerate(self.input_layer.getFeatures(), start=1):
            geometry = QgsGeometry(feature.geometry())
            if geometry.isNull() or geometry.isEmpty():
                raise ValueError(f"Input feature {feature.id()} has an empty geometry.")
            if transform is not None:
                geometry.transform(transform)

            area = self._feature_area(feature, geometry, self.config.input_area_field, "input")
            values = {
                spec.source_field: to_finite_float(feature[spec.source_field])
                for spec in self.variable_specs
            }
            weight_value = None
            if alt_input_field:
                raw_weight = feature[alt_input_field]
                if (
                    is_null(raw_weight)
                    and self.alternative is not None
                    and self.alternative.variable_type == WEIGHT_EXTENSIVE
                ):
                    weight_value = 0.0
                    input_uid = feature[self.config.input_uid_field]
                    self._add_warning(
                        "NULL_EXTENSIVE_WEIGHT",
                        f"Input polygon UID '{input_uid}' has a null extensive weighting value "
                        f"in field '{alt_input_field}'. Its source weight was set to 0.",
                        key=f"input:{input_uid}",
                    )
                else:
                    weight_value = to_nonnegative_float(raw_weight)
                    if weight_value is None:
                        raise ValueError(
                            f"Input feature {feature.id()} has a null, nonnumeric, or negative "
                            f"alternative weight in field '{alt_input_field}'."
                        )

            record = InputRecord(
                fid=int(feature.id()),
                uid=feature[self.config.input_uid_field],
                geometry=geometry,
                area_m2=area,
                values=values,
                weight_value=weight_value,
            )
            records[record.fid] = record

            indexed_feature = QgsFeature()
            indexed_feature.setId(record.fid)
            indexed_feature.setGeometry(geometry)
            index.addFeature(indexed_feature)

            if input_count > 0 and position % 500 == 0:
                self.progress(int(15 * position / input_count))

        return records, index

    def _requires_alternative_input_totals(self) -> bool:
        return (
            not self.template_only
            and self.config.weighting_strategy == WEIGHT_ALTERNATIVE
            and any(spec.category == "Extensive" for spec in self.variable_specs)
        )

    def _precompute_input_weight_totals(
        self,
        input_records: Dict[int, InputRecord],
        spatial_index: QgsSpatialIndex,
    ) -> Dict[str, float]:
        totals: Dict[str, float] = {}
        contributors: set = set()
        output_count = int(self.output_layer.featureCount())

        for position, output_feature in enumerate(self.output_layer.getFeatures(), start=1):
            output_geometry = self._geometry_in_calculation_crs(
                output_feature.geometry(), self.output_layer.crs(), "output"
            )
            if output_geometry.isNull() or output_geometry.isEmpty():
                raise ValueError(f"Output feature {output_feature.id()} has an empty geometry.")
            output_uid = output_feature[self.config.output_uid_field]
            output_weight_value = self._output_weight_value(output_feature)

            for input_fid in spatial_index.intersects(output_geometry.boundingBox()):
                record = input_records.get(int(input_fid))
                if record is None or not output_geometry.intersects(record.geometry):
                    continue
                intersection = output_geometry.intersection(record.geometry)
                if intersection.isNull() or intersection.isEmpty():
                    continue
                area = float(self.distance_area.measureArea(intersection))
                if not math.isfinite(area) or area <= 0:
                    continue

                key = self._compose_intersection_uid(output_uid, record.uid)
                weight, _, _, _ = self._alternative_fragment_weight(
                    record,
                    output_weight_value,
                    key,
                    area,
                )
                input_key = str(record.uid)
                contributors.add(input_key)
                totals[input_key] = totals.get(input_key, 0.0) + weight

            if output_count > 0 and position % 100 == 0:
                self.progress(15 + int(15 * position / output_count))

        zero_totals = sorted(key for key in contributors if totals.get(key, 0.0) <= 0.0)
        for input_key in zero_totals:
            self._add_warning(
                "ZERO_INPUT_WEIGHT_TOTAL",
                f"Input polygon UID '{input_key}' has total extensive alternative weight 0 "
                "across its intersection features. Its extensive target contributions are "
                "set to 0.",
                key=f"input-total:{input_key}",
            )
        return totals

    def _process_output_feature(
        self,
        output_feature: QgsFeature,
        input_records: Dict[int, InputRecord],
        spatial_index: QgsSpatialIndex,
        intersection_fields: QgsFields,
        intersection_writer: QgsVectorFileWriter,
        output_fields: Optional[QgsFields],
    ) -> Tuple[Optional[QgsFeature], int]:
        output_geometry = self._geometry_in_calculation_crs(
            output_feature.geometry(), self.output_layer.crs(), "output"
        )
        if output_geometry.isNull() or output_geometry.isEmpty():
            raise ValueError(f"Output feature {output_feature.id()} has an empty geometry.")

        output_area = self._feature_area(
            output_feature,
            output_geometry,
            self.config.output_area_field,
            "output",
        )
        output_uid = output_feature[self.config.output_uid_field]
        output_weight_value = self._output_weight_value(output_feature)

        accumulators = self._empty_variable_accumulators()

        total_intersection_area = 0.0
        relationship_count = 0

        for input_fid in spatial_index.intersects(output_geometry.boundingBox()):
            record = input_records.get(int(input_fid))
            if record is None or not output_geometry.intersects(record.geometry):
                continue

            intersection = output_geometry.intersection(record.geometry)
            if intersection.isNull() or intersection.isEmpty():
                continue
            intersection_area = float(self.distance_area.measureArea(intersection))
            if not math.isfinite(intersection_area) or intersection_area <= 0.0:
                continue

            relationship_count += 1
            total_intersection_area += intersection_area
            ra_output = intersection_area / output_area
            ra_input = intersection_area / record.area_m2
            intersection_uid = self._register_intersection_uid(output_uid, record.uid)

            fragment_weight = None
            weight_src_input = None
            weight_src_output = None
            weight_src_intersection = None
            if self.config.weighting_strategy == WEIGHT_ALTERNATIVE:
                if self.template_only:
                    weight_src_input = record.weight_value
                    weight_src_output = output_weight_value
                else:
                    (
                        fragment_weight,
                        weight_src_input,
                        weight_src_output,
                        weight_src_intersection,
                    ) = self._alternative_fragment_weight(
                        record,
                        output_weight_value,
                        intersection_uid,
                        intersection_area,
                    )

            intersection_feature = QgsFeature(intersection_fields)
            intersection_feature.setGeometry(force_multipolygon(intersection))
            intersection_feature[self.config.intersection_uid_field] = intersection_uid
            intersection_feature["input_uid"] = record.uid
            intersection_feature["output_uid"] = output_uid
            intersection_feature["if_area_m2"] = intersection_area
            intersection_feature["ip_area_m2"] = record.area_m2
            intersection_feature["op_area_m2"] = output_area
            intersection_feature["ra_input"] = ra_input
            intersection_feature["ra_output"] = ra_output

            if self.config.weighting_strategy == WEIGHT_ALTERNATIVE:
                self._set_optional_field(
                    intersection_feature,
                    intersection_fields,
                    self.weight_source_input_field,
                    weight_src_input,
                )
                self._set_optional_field(
                    intersection_feature,
                    intersection_fields,
                    self.weight_source_output_field,
                    weight_src_output,
                )
                self._set_optional_field(
                    intersection_feature,
                    intersection_fields,
                    self.weight_source_intersection_field,
                    weight_src_intersection,
                )
                if self.template_only:
                    assert self.alternative is not None
                    intersection_feature[self.alternative.template_weight_field] = NULL
                else:
                    self._set_optional_field(
                        intersection_feature,
                        intersection_fields,
                        self.fragment_weight_field,
                        fragment_weight,
                    )

            for spec in self.variable_specs:
                value = record.values[spec.source_field]
                intersection_feature[spec.intersection_source_field] = value
                if value is None or self.template_only:
                    if spec.intersection_value_field:
                        intersection_feature[spec.intersection_value_field] = NULL
                    continue

                accumulator = accumulators[spec.source_field]
                accumulator["count"] += 1.0

                if self.config.weighting_strategy == WEIGHT_AREA:
                    if spec.category == "Intensive":
                        accumulator["numerator"] += value * ra_output
                        accumulator["denominator"] += ra_output
                    else:
                        fragment_value = value * ra_input
                        accumulator["sum"] += fragment_value
                        assert spec.intersection_value_field is not None
                        intersection_feature[spec.intersection_value_field] = fragment_value
                else:
                    assert fragment_weight is not None
                    if spec.category == "Intensive":
                        accumulator["numerator"] += value * fragment_weight
                        accumulator["denominator"] += fragment_weight
                    else:
                        denominator = self.input_weight_totals.get(str(record.uid), 0.0)
                        if denominator <= 0.0:
                            fragment_value = 0.0
                            accumulator["forced_zero_count"] += 1.0
                            self._add_warning(
                                "ZERO_INPUT_WEIGHT_TOTAL",
                                f"Input polygon UID '{record.uid}' has total extensive "
                                "alternative weight 0. Its contribution to output polygon "
                                f"UID '{output_uid}' for variable '{spec.source_field}' was set "
                                "to 0.",
                                key=(
                                    f"allocation:{record.uid}:{output_uid}:"
                                    f"{spec.source_field}"
                                ),
                            )
                        else:
                            fragment_value = value * fragment_weight / denominator
                        accumulator["sum"] += fragment_value
                        assert spec.intersection_value_field is not None
                        intersection_feature[spec.intersection_value_field] = fragment_value

            if not intersection_writer.addFeature(intersection_feature, QgsFeatureSink.FastInsert):
                raise RuntimeError("Could not write an intersection feature.")

        if self.template_only:
            return None, relationship_count

        assert output_fields is not None
        result = QgsFeature(output_fields)
        result.setGeometry(output_geometry)
        for field_name in self.output_layer.fields().names():
            result[field_name] = output_feature[field_name]

        result[self.coverage_field] = (
            total_intersection_area / output_area if relationship_count > 0 else 0.0
        )
        result[self.input_count_field] = relationship_count

        for spec in self.variable_specs:
            accumulator = accumulators[spec.source_field]
            if accumulator["count"] == 0:
                result[spec.output_field] = NULL
            elif spec.category == "Intensive":
                denominator = accumulator["denominator"]
                if denominator > 0:
                    result[spec.output_field] = accumulator["numerator"] / denominator
                elif self.config.weighting_strategy == WEIGHT_ALTERNATIVE:
                    result[spec.output_field] = 0.0
                    self._add_warning(
                        "ZERO_OUTPUT_WEIGHT_DENOMINATOR",
                        f"Output polygon UID '{output_uid}' has total alternative weight 0 "
                        f"for intensive variable '{spec.source_field}'. The reprojected value "
                        "was set to 0.",
                        key=f"output-denominator:{output_uid}:{spec.source_field}",
                    )
                else:
                    result[spec.output_field] = NULL
            else:
                result[spec.output_field] = accumulator["sum"]
                if (
                    self.config.weighting_strategy == WEIGHT_ALTERNATIVE
                    and accumulator["sum"] == 0.0
                    and accumulator["forced_zero_count"] > 0.0
                ):
                    self._add_warning(
                        "ZERO_OUTPUT_VALUE_FROM_ZERO_WEIGHTS",
                        f"Output polygon UID '{output_uid}' received a reprojected value of "
                        f"0 for extensive variable '{spec.source_field}' because one or more "
                        "contributing input polygons had total alternative weight 0.",
                        key=f"output-zero:{output_uid}:{spec.source_field}",
                    )

        return result, relationship_count

    def _output_weight_value(self, output_feature: QgsFeature) -> Optional[float]:
        if self.alternative is None or not self.alternative.output_field:
            return None
        raw_weight = output_feature[self.alternative.output_field]
        if is_null(raw_weight) and self.alternative.variable_type == WEIGHT_EXTENSIVE:
            output_uid = output_feature[self.config.output_uid_field]
            self._add_warning(
                "NULL_EXTENSIVE_WEIGHT",
                f"Output polygon UID '{output_uid}' has a null extensive weighting value "
                f"in field '{self.alternative.output_field}'. Its source weight was set to 0.",
                key=f"output:{output_uid}",
            )
            return 0.0
        value = to_nonnegative_float(raw_weight)
        if value is None:
            raise ValueError(
                f"Output feature {output_feature.id()} has a null, nonnumeric, or negative "
                f"alternative weight in field '{self.alternative.output_field}'."
            )
        return value

    def _alternative_fragment_weight(
        self,
        record: InputRecord,
        output_weight_value: Optional[float],
        intersection_uid: str,
        intersection_area: float,
    ) -> Tuple[float, Optional[float], Optional[float], Optional[float]]:
        alt = self.alternative
        if alt is None:
            raise RuntimeError("Alternative weighting is not configured.")

        source_input = record.weight_value
        source_output = output_weight_value
        source_intersection = None

        if alt.availability == SOURCE_INPUT:
            assert source_input is not None
            intensity = source_input
            weight = intensity * intersection_area
        elif alt.availability == SOURCE_OUTPUT:
            assert source_output is not None
            intensity = source_output
            weight = intensity * intersection_area
        elif alt.availability == SOURCE_BOTH:
            assert source_input is not None and source_output is not None
            intensity = (source_input + source_output) / 2.0
            weight = intensity * intersection_area
        elif alt.availability == SOURCE_INTERSECTION:
            external = self.external_weights.get(intersection_uid)
            if external is None:
                raise ValueError(
                    f"No external weight was found for intersection key '{intersection_uid}'."
                )
            source_intersection, external_input_uid = external
            if external_input_uid != str(record.uid):
                raise ValueError(
                    f"External input UID mismatch for intersection '{intersection_uid}'. "
                    f"Expected '{record.uid}', found '{external_input_uid}'."
                )
            self.used_external_weight_keys.add(intersection_uid)
            if alt.variable_type == WEIGHT_INTENSIVE:
                weight = source_intersection * intersection_area
            else:
                weight = source_intersection
        else:
            raise RuntimeError(f"Unsupported alternative weight source: {alt.availability}")

        if not math.isfinite(weight) or weight < 0.0:
            raise ValueError(
                f"Invalid fragment weight for intersection '{intersection_uid}': {weight}"
            )
        return weight, source_input, source_output, source_intersection

    @staticmethod
    def _set_optional_field(
        feature: QgsFeature,
        fields: QgsFields,
        field_name: str,
        value: Optional[float],
    ) -> None:
        if fields.indexOf(field_name) >= 0:
            feature[field_name] = value if value is not None else NULL

    def _add_warning(self, code: str, message: str, key: Optional[str] = None) -> None:
        warning_key = f"{code}|{key if key is not None else message}"
        if warning_key in self._warning_keys:
            return
        self._warning_keys.add(warning_key)
        entry = f"[{code}] {message}"
        self.warning_messages.append(entry)
        self.log("Warning: " + message)

    def _write_warning_log(
        self, destination: Path, summary: Dict[str, Any]
    ) -> Path:
        log_path = destination.with_name(destination.stem + "_warnings.log")
        lines = [
            "Grid transfer report and warning log",
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            f"Output GeoPackage: {destination}",
            f"Target CRS: {self.calculation_crs.authid() or self.config.target_crs}",
            f"Weighting strategy: {self.config.weighting_strategy}",
            f"Weight used: {describe_weighting_configuration(self.config.weighting_strategy, self.config.alternative_weight)}",
            f"Reprojected field prefix: {self.config.output_field_prefix or '[none]'}",
            (
                "Selected target variables, not reprojected in template-only session:"
                if summary.get("template_only")
                else "Reprojected variables:"
            ),
            *(
                f"  {spec.source_field} -> {spec.output_field} [{spec.category}]"
                for spec in self.variable_specs
            ),
            f"Output polygons: {summary['output_features']}",
            f"Intersection relationships: {summary['intersections']}",
            f"Uncovered output polygons: {summary['uncovered_outputs']}",
            f"Warning entries: {len(self.warning_messages)}",
            "",
        ]
        if self.warning_messages:
            lines.append("Warnings")
            lines.append("--------")
            lines.extend(self.warning_messages)
        else:
            lines.append("No warnings were generated.")
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return log_path

    def _compose_intersection_uid(self, output_uid: Any, input_uid: Any) -> str:
        return f"{str(output_uid)}_{str(input_uid)}"

    def _register_intersection_uid(self, output_uid: Any, input_uid: Any) -> str:
        output_text = str(output_uid)
        input_text = str(input_uid)
        combined_uid = f"{output_text}_{input_text}"
        pair = (output_text, input_text)

        existing_pair = self._intersection_uid_pairs.get(combined_uid)
        if existing_pair is not None:
            if existing_pair == pair:
                raise ValueError(
                    "The same input and output polygon pair generated more than one "
                    f"intersection relationship: {combined_uid}"
                )
            raise ValueError(
                "The generated intersection UID is not unique. "
                f"'{combined_uid}' is produced by both output/input pairs "
                f"{existing_pair} and {pair}. Modify the source UID values or use "
                "unambiguous UID fields."
            )
        self._intersection_uid_pairs[combined_uid] = pair
        return combined_uid

    def _geometry_in_calculation_crs(
        self, geometry: QgsGeometry, source_crs: QgsCoordinateReferenceSystem, label: str
    ) -> QgsGeometry:
        transformed = QgsGeometry(geometry)
        if transformed.isNull() or transformed.isEmpty():
            raise ValueError(f"A {label} feature has an empty geometry.")
        if source_crs != self.calculation_crs:
            transform = QgsCoordinateTransform(
                source_crs, self.calculation_crs, QgsProject.instance()
            )
            transformed.transform(transform)
        return transformed

    def _feature_area(
        self,
        feature: QgsFeature,
        geometry: QgsGeometry,
        area_field: Optional[str],
        label: str,
    ) -> float:
        if area_field:
            area = to_finite_float(feature[area_field])
            if area is None or area <= 0:
                raise ValueError(
                    f"Feature {feature.id()} in the {label} grid has an invalid area in "
                    f"field '{area_field}'. Existing area fields must contain positive square metres."
                )
            return area

        area = float(self.distance_area.measureArea(geometry))
        if not math.isfinite(area) or area <= 0:
            raise ValueError(f"Feature {feature.id()} in the {label} grid has invalid geometry area.")
        return area

    def _intersection_fields(self) -> QgsFields:
        fields = QgsFields()
        fields.append(
            QgsField(self.config.intersection_uid_field, QVariant.String, "text", 512, 0)
        )
        fields.append(
            clone_field(
                self.input_layer.fields().field(self.config.input_uid_field),
                "input_uid",
            )
        )
        fields.append(
            clone_field(
                self.output_layer.fields().field(self.config.output_uid_field),
                "output_uid",
            )
        )
        fields.append(QgsField("if_area_m2", QVariant.Double, "double", 20, 6))
        fields.append(QgsField("ip_area_m2", QVariant.Double, "double", 20, 6))
        fields.append(QgsField("op_area_m2", QVariant.Double, "double", 20, 6))
        fields.append(QgsField("ra_input", QVariant.Double, "double", 20, 12))
        fields.append(QgsField("ra_output", QVariant.Double, "double", 20, 12))

        if self.config.weighting_strategy == WEIGHT_ALTERNATIVE:
            alt = self.alternative
            assert alt is not None
            if alt.availability in (SOURCE_INPUT, SOURCE_BOTH):
                fields.append(
                    QgsField(self.weight_source_input_field, QVariant.Double, "double", 20, 10)
                )
            if alt.availability in (SOURCE_OUTPUT, SOURCE_BOTH):
                fields.append(
                    QgsField(self.weight_source_output_field, QVariant.Double, "double", 20, 10)
                )
            if alt.availability == SOURCE_INTERSECTION:
                fields.append(
                    QgsField(
                        self.weight_source_intersection_field,
                        QVariant.Double,
                        "double",
                        20,
                        10,
                    )
                )
            if self.template_only:
                existing_names = {name.lower() for name in self.intersection_used_names}
                if alt.template_weight_field.lower() in existing_names:
                    raise ValueError(
                        f"Template field name '{alt.template_weight_field}' conflicts with an "
                        "existing intersection field."
                    )
                fields.append(
                    QgsField(alt.template_weight_field, QVariant.Double, "double", 20, 10)
                )
            else:
                fields.append(
                    QgsField(self.fragment_weight_field, QVariant.Double, "double", 20, 10)
                )

        for spec in self.variable_specs:
            source_field = self.input_layer.fields().field(spec.source_field)
            fields.append(clone_field(source_field, spec.intersection_source_field))
            if spec.intersection_value_field:
                fields.append(
                    QgsField(spec.intersection_value_field, QVariant.Double, "double", 20, 10)
                )
        return fields

    def _output_fields(self) -> QgsFields:
        fields = QgsFields()
        for field in self.output_layer.fields():
            fields.append(QgsField(field))
        fields.append(QgsField(self.coverage_field, QVariant.Double, "double", 20, 12))
        fields.append(QgsField(self.input_count_field, QVariant.Int, "integer", 10, 0))
        for spec in self.variable_specs:
            fields.append(QgsField(spec.output_field, QVariant.Double, "double", 20, 10))
        return fields

    def _create_writer(
        self,
        destination: str,
        layer_name: str,
        fields: QgsFields,
        geometry_type: int,
        crs,
        overwrite_file: bool,
    ) -> QgsVectorFileWriter:
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = layer_name
        options.fileEncoding = "UTF-8"
        options.actionOnExistingFile = (
            QgsVectorFileWriter.CreateOrOverwriteFile
            if overwrite_file
            else QgsVectorFileWriter.CreateOrOverwriteLayer
        )

        writer = QgsVectorFileWriter.create(
            destination,
            fields,
            geometry_type,
            crs,
            self.context,
            options,
        )
        if writer is None:
            raise RuntimeError(f"Could not create output layer '{layer_name}'.")
        if writer.hasError() != QgsVectorFileWriter.NoError:
            error_message = writer.errorMessage()
            del writer
            raise RuntimeError(f"Could not create '{layer_name}': {error_message}")
        return writer


def choose_polygon_source(parent: QWidget, title: str) -> Optional[str]:
    """Browse vector data and select a polygon sublayer when a container has several."""
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
        return polygon_details[labels.index(selected)].uri()
    except Exception:
        # Provider URI discovery is not available in all QGIS builds. Falling
        # back to the container path preserves compatibility with single-layer
        # GeoPackages.
        return path


def populate_numeric_combo(combo: QComboBox, layer: QgsVectorLayer) -> None:
    combo.clear()
    combo.addItem("<Select field>", None)
    for field in layer.fields():
        if is_numeric_field(field):
            combo.addItem(field.name(), field.name())


def set_combo_text(combo: QComboBox, text: Optional[str]) -> None:
    if not text:
        return
    index = combo.findText(text)
    if index >= 0:
        combo.setCurrentIndex(index)


def set_combo_data(combo: QComboBox, data: Optional[str]) -> None:
    if not data:
        return
    index = combo.findData(data)
    if index >= 0:
        combo.setCurrentIndex(index)


def describe_weighting_configuration(
    strategy: str,
    config: Optional[AlternativeWeightConfiguration],
) -> str:
    """Return a concise description of the weight used in the session."""
    if strategy == WEIGHT_AREA:
        return "intersection area"

    if config is None:
        return "alternative weighting, configuration unavailable"

    parts = [
        f"alternative {config.variable_type.lower()} variable",
        f"source={config.availability}",
    ]
    if config.input_field:
        parts.append(f"input field={config.input_field}")
    if config.output_field:
        parts.append(f"output field={config.output_field}")
    if config.intersection_weight_field:
        parts.append(f"intersection field={config.intersection_weight_field}")
    if config.template_only:
        parts.append(f"template field={config.template_weight_field}")
    return "; ".join(parts)


def is_valid_field_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def is_numeric_field(field: QgsField) -> bool:
    """Return True for numeric QGIS fields across QGIS and PyQt versions.

    QgsField.type() may expose QVariant.Type or QMetaType.Type depending on the
    QGIS and Qt binding version. QgsField.isNumeric(), when available, is the
    most reliable test. The remaining checks provide compatibility with older
    installations.
    """
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
    numeric_type_names = (
        "int",
        "integer",
        "smallint",
        "bigint",
        "int2",
        "int4",
        "int8",
        "real",
        "float",
        "double",
        "numeric",
        "decimal",
    )
    return any(token in type_name for token in numeric_type_names)


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


def to_nonnegative_float(value: Any) -> Optional[float]:
    numeric = to_finite_float(value)
    if numeric is None or numeric < 0.0:
        return None
    return numeric


def clone_field(field: QgsField, new_name: str) -> QgsField:
    cloned = QgsField(field)
    cloned.setName(new_name)
    return cloned


def unique_field_name(base: str, used: set, max_length: int = 60) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", base).strip("_") or "field"
    if sanitized[0].isdigit():
        sanitized = "f_" + sanitized
    sanitized = sanitized[:max_length]
    candidate = sanitized
    counter = 2
    lower_used = {name.lower() for name in used}
    while candidate.lower() in lower_used:
        suffix = f"_{counter}"
        candidate = sanitized[: max_length - len(suffix)] + suffix
        counter += 1
    used.add(candidate)
    return candidate


def force_multipolygon(geometry: QgsGeometry) -> QgsGeometry:
    geom = QgsGeometry(geometry)
    if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.PolygonGeometry:
        polygon_parts = []
        for part in geom.asGeometryCollection():
            if QgsWkbTypes.geometryType(part.wkbType()) == QgsWkbTypes.PolygonGeometry:
                polygon_parts.append(part)
        if not polygon_parts:
            return QgsGeometry()
        geom = QgsGeometry.unaryUnion(polygon_parts)
    if not QgsWkbTypes.isMultiType(geom.wkbType()):
        geom.convertToMultiType()
    return geom


def main() -> int:
    QgsApplication.setPrefixPath(QGIS_PREFIX, True)
    qgis_argv = [os.fsencode(argument) for argument in sys.argv]
    app = QgsApplication(qgis_argv, True)
    app.initQgis()

    try:
        window = GridTransferWindow()
        window.show()
        if hasattr(app, "exec_"):
            return app.exec_()
        return app.exec()
    finally:
        app.exitQgis()


if __name__ == "__main__":
    sys.exit(main())