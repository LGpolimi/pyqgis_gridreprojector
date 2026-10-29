# Grid Reprojector

**Grid Reprojector** is a standalone PyQGIS application for transferring numerical information between polygon spatial supports. It provides three workflows in one graphical interface:

1. **Base version**, transfer one polygon dataset to a destination polygon grid.
2. **Multifile version**, repeat the base workflow automatically across multiple input vector files in the same directory.
3. **Timeseries version**, transfer a time-by-space CSV matrix from an input polygon grid to an output polygon grid while calculating the spatial relationships only once.

The software is intended for spatial harmonization and areal interpolation workflows in which variables from different territorial subdivisions must be represented on a common spatial support.

## Version

Current release: **1.0.0**

## Main features

### Base version

- Polygon-to-polygon transfer using computed intersections or an existing intersection layer.
- User-selectable input and output UID fields.
- User-selectable area fields, or geometry-based area calculation.
- Common processing CRS, editable in the GUI.
- Numerical variable selection with intensive/extensive classification.
- Large searchable variable-selection dialog.
- Area-based transfer.
- Alternative weighting using input-grid, output-grid, combined, or intersection-level information where methodologically supported.
- Configurable prefix for transferred output fields.
- Warning/report log.
- Output as:
  - GeoPackage (`.gpkg`), default;
  - ESRI Shapefile (`.shp`);
  - CSV (`.csv`).

### Multifile version

- Uses the complete Base version configuration.
- The selected input dataset acts as the schema/configuration example.
- Automatically discovers supported vector files in the same directory.
- Filters files using a user-defined filename key string.
- By default, the key is the extension of the selected example file, e.g. `.gpkg`.
- Writes each result as `<output prefix><input stem><selected output extension>`.
- Continues processing remaining files if one file fails.
- Produces a multifile summary log.

### Timeseries version

- Accepts a CSV matrix with one row per temporal observation and one column per input-grid polygon.
- Matches CSV spatial column names to the selected input-grid UID field.
- Preserves the selected time/key column.
- Replaces the input-grid columns with destination-grid UID columns.
- Computes or loads spatial intersections once and reuses them for every temporal row.
- Supports intensive and extensive area-based transfer.
- Optional export of the reusable spatial weights table.

---

# Methodology

## Notation

For input polygon `i` and output polygon `j`:

- `A_i` = area of input polygon `i`;
- `A_j` = area of output polygon `j`;
- `A_ij` = positive-area intersection between `i` and `j`;
- `X_i` = source variable value;
- `Y_j` = transferred output value.

## Area weighting, intensive variables

Intensive variables are transferred using a normalized area-weighted mean:

```text
Y_j = sum_i(X_i * A_ij) / sum_i(A_ij)
```

The normalization means that, when an output polygon is only partially covered by valid contributors, the estimate refers to the covered portion rather than being artificially reduced by uncovered area.

## Area weighting, extensive variables

Extensive variables use the covered-share allocation rule:

```text
VV_ij = X_i * A_ij / A_i
Y_j   = sum_i(VV_ij)
```

Therefore, an input polygon that is only partially covered by the destination grid transfers only the covered share of its total value. The software does not automatically redistribute an uncovered remainder.

## Alternative weighting in Base and Multifile modes

A non-negative fragment weight `W_ij` can replace ordinary area weighting.

For intensive targets:

```text
Y_j = sum_i(X_i * W_ij) / sum_i(W_ij)
```

For extensive targets:

```text
VV_ij = X_i * W_ij / sum_j(W_ij)
Y_j   = sum_i(VV_ij)
```

For an **intensive weighting variable**:

- input grid only: `W_ij = Z_i * A_ij`;
- output grid only: `W_ij = Z_j * A_ij`;
- both grids: `W_ij = ((Z_i + Z_j) / 2) * A_ij`;
- existing intersection layer: `W_ij = Z_ij * A_ij`.

For an **extensive weighting variable**, direct use requires an extensive fragment weight at intersection level. If an extensive weight is available only at polygon level, the Base version can create an intersection template to be populated externally before rerunning the transfer.

A null extensive weighting value is treated as zero and reported in the warning log. If an alternative-weight denominator is zero, the corresponding result is set to zero according to the implemented zero-weight rules and the condition is logged.

**Important:** an intensive weighting variable available only on the output grid is constant across all contributors to that output polygon. For intensive targets it therefore cancels during normalization and gives the same result as ordinary area weighting. The GUI warns about this case.

## Timeseries transfer

For a temporal value `X_ti` at time `t`, intensive transfer is:

```text
Y_tj = sum_i(X_ti * A_ij) / sum_i(A_ij)
```

The denominator is recalculated for each temporal row using only contributors with valid numerical values.

For an extensive temporal variable:

```text
Y_tj = sum_i(X_ti * A_ij / A_i)
```

Spatial intersections are computed once and reused for all rows.

---

# Requirements

## Required software

- **QGIS 3.x** with PyQGIS.
- A Python interpreter compatible with that QGIS installation. Using the Python environment supplied with QGIS is strongly recommended.

The application has **no additional pip dependencies**. It uses the Python standard library plus PyQGIS/PyQt supplied by QGIS.

## Runtime portability

No QGIS installation path is hard-coded. `qgis_runtime.py` discovers the runtime in the following order:

1. `--qgis-prefix` command-line option;
2. `GRIDREPROJECTOR_QGIS_PREFIX` environment variable;
3. `QGIS_PREFIX_PATH` or `QGIS_PREFIX` environment variables;
4. an already importable `qgis` Python package;
5. `OSGEO4W_ROOT` or `OSGEO4W_HOME` environment variables;
6. QGIS executables available on `PATH`.

The discovery logic supports common OSGeo4W-style installations and already configured QGIS Python environments on Windows, Linux, and macOS.

A runtime path can be supplied explicitly if automatic discovery fails:

```bash
python main.py --qgis-prefix "<QGIS_PREFIX_OR_INSTALLATION_ROOT>"
```

or through an environment variable:

```text
GRIDREPROJECTOR_QGIS_PREFIX=<QGIS_PREFIX_OR_INSTALLATION_ROOT>
```

The Python interpreter still needs to be ABI-compatible with the installed QGIS build. Runtime path discovery cannot make an unrelated Python installation binary-compatible with PyQGIS.

---

# Installation

1. Install QGIS, if it is not already installed.
2. Download or clone the Grid Reprojector release.
3. Keep the application files together in the same directory:

```text
GridReprojector/
├── main.py
├── qgis_runtime.py
├── base_reprojector.py
├── timeseries_reprojector.py
├── requirements.txt
├── README.md
├── LICENSE.txt
└── CITATION.cff
```

4. Open a terminal in that directory using a Python environment compatible with QGIS.
5. Run:

```bash
python main.py
```

On Windows, an OSGeo4W/QGIS shell is generally the simplest environment. On Linux or macOS, use the Python environment in which `import qgis` succeeds, or supply the QGIS prefix explicitly.

To verify the release version without initializing QGIS:

```bash
python main.py --version
```

To show command-line startup options:

```bash
python main.py --help
```

---

# Input data requirements

## Polygon grids

Input, destination, and optional intersection layers must be polygon layers.

For every grid:

- select a UID field;
- UID values must be non-null and unique;
- the layer must have a valid CRS;
- geometries must have positive area.

If an existing area field is selected, its values are assumed to be **square metres** and must be positive. If no area field is selected, the program calculates area from geometry after transformation to the selected processing CRS.

The default processing CRS is `EPSG:32632`, but it is editable. Users outside the corresponding UTM zone should select an appropriate projected CRS for their study area. A suitable metric CRS is strongly recommended for robust overlay and area calculations.

### Geometry validity

The application rejects null or empty geometries, but it does not automatically repair topologically invalid polygons. Invalid geometry can cause GEOS intersection operations to fail or produce unusable intersections. Validate and, if necessary, repair source geometries before processing.

## GeoPackages with multiple polygon layers

When a selected GeoPackage contains multiple polygon layers, the application presents a layer-selection dialog. This applies to grid layers and existing intersection/weight layers.

---

# Using the Base version

1. Open the **Base version** tab.
2. Select the **Input grid**.
3. Select its **UID field** and optionally an existing square-metre **Area field**.
4. Select the **Output grid** and its UID/area fields.
5. Select the **Common processing CRS**.
6. Choose the **Intersection source**:
   - `Compute intersections from grids`, or
   - `Use existing intersection layer`.
7. Select numerical variables under **Variables to transfer**.
8. Classify each selected variable as:
   - `Intensive`, or
   - `Extensive`.
9. For datasets with many fields, click **Open larger variable selector** and use the search box to filter field names.
10. Select the weighting strategy:
    - `Area based`, or
    - `Alternative variable` and configure the weight source.
11. Under **Output**:
    - choose `GeoPackage`, `Shapefile`, or `CSV`;
    - define the intersection UID field name;
    - define the prefix used for reprojected attributes, default `tr`;
    - choose the destination file.
12. Click **Run grid transfer**.

## Base-version outputs

### GeoPackage

The default GeoPackage contains:

- `grid_intersections`;
- `reprojected_output`.

In extensive-weight template-only mode, only the intersection template is created.

### Shapefile

The selected `.shp` contains the reprojected output. A companion file is created:

```text
<output_stem>_grid_intersections.shp
```

In template-only mode, the selected Shapefile is the intersection template.

**Shapefile limitation:** DBF field names are format constrained and may be shortened by the driver. Use GeoPackage when preserving long field names is important.

### CSV

The selected `.csv` contains the reprojected output attribute table without geometry. A companion table is created:

```text
<output_stem>_grid_intersections.csv
```

In template-only mode, the selected CSV is the intersection template.

For CSV or Shapefile output, the established Base engine first creates its standard temporary GeoPackage and then exports the generated layers. This preserves the same transfer mathematics and quality-control logic across output formats.

Before processing starts, the application verifies that the requested primary and companion output files do not coincide with the input grid, destination grid, existing intersection layer, or external weight source. This prevents accidental deletion or overwrite of source vector data.

### Warning log

A log is written next to the primary output:

```text
<output_stem>_warnings.log
```

It records the transfer configuration, variables, weighting method, output prefix, processing summary, and any detected warning conditions.

---

# Using the Multifile version

The Multifile page repeats the Base workflow for a set of input files sharing the same schema.

## Assumptions

All iterated files are expected to use the same relevant structure as the selected example input, including:

- polygon geometry type;
- selected UID field name;
- selected variable field names;
- selected area field name, if used;
- selected weighting field name, if used;
- for multi-layer GeoPackages, the same selected layer name/URI suffix.

Files that do not satisfy the shared configuration are reported as failures while the remaining files continue.

## Procedure

1. Open the **Multifile version** tab.
2. Configure the input and output grids exactly as in the Base version.
3. The selected input grid acts as the **example input file**.
4. Configure variables, weighting, intersection source, CRS, and output format.
5. Under **Multifile iteration**:
   - set the **Filename key string**;
   - select the **Output folder**;
   - set the **Output filename prefix**.
6. Click **Run multifile grid transfer**.

The program examines the directory containing the selected example input file and includes supported `.gpkg` and `.shp` files whose **filename contains the key string**, using case-insensitive matching.

The default key is the selected example file extension, for example:

```text
.gpkg
```

Known destination-grid files and configured intersection/weight source files are excluded from the iteration set.

## Output filenames

For an input file:

```text
example_2025.gpkg
```

with output prefix:

```text
tr_
```

GeoPackage output is:

```text
tr_example_2025.gpkg
```

If CSV or Shapefile is selected, the extension is changed accordingly.

Before a batch starts, all primary and companion output paths are checked against every candidate input file and configured source layer. The batch is blocked if an output would replace any source file. This also protects against common same-folder/same-extension naming collisions.

A batch report is written to the output directory:

```text
<output_prefix>multifile_report.log
```

It lists discovered, successful, and failed files.

---

# Using the Timeseries version

The Timeseries page is designed for a time-by-space matrix in which each spatial column corresponds to one polygon in the input grid.

## Expected CSV structure

Example:

```csv
DATE_STR,9,10,40,41
200101,0.45,0.44,0.51,0.48
200102,1.01,0.94,1.10,1.04
```

- One row = one temporal observation.
- One selected column = time/key value.
- Every other spatial column name is matched to an input-grid UID.

The output preserves the same row structure but replaces input-grid columns with output-grid UID columns:

```csv
DATE_STR,<output_uid_1>,<output_uid_2>,...
200101,...
200102,...
```

## Procedure

1. Open the **Timeseries version** tab.
2. Select the input polygon grid and its UID/area fields.
3. Select the output polygon grid and its UID/area fields.
4. Select the processing CRS.
5. Choose whether to compute intersections or use an existing intersection layer.
6. Select the temporal CSV.
7. Select or auto-detect the delimiter.
8. Select the **Time/key column**. `DATE_STR` is proposed automatically when present.
9. Select the variable type:
   - `Intensive`, or
   - `Extensive`.
10. Choose whether exact UID coverage between grid and CSV columns is required.
11. Select the output CSV.
12. Optionally enable spatial-weight export.
13. Click **Run temporal grid transfer**.

The output CSV must be different from the source temporal CSV. The application explicitly blocks source/output identity to prevent accidental truncation of the input matrix.

## UID matching

The Timeseries workflow canonicalizes numerical identifiers so, for example, an integer grid UID `9` can match a CSV column named `9` or a numerically equivalent textual representation when the mapping is unambiguous.

With strict UID coverage enabled, processing stops if:

- a spatial CSV column does not correspond to an input-grid UID; or
- an input-grid UID has no CSV column.

With strict coverage disabled, unmatched columns and input polygons are ignored and reported.

## Missing values

The following textual values are treated as missing:

```text
blank, NA, NaN, null, none, nodata
```

For an intensive variable, missing contributors are excluded from both numerator and denominator, so valid contributors are renormalized for that temporal row.

For an extensive variable, missing contributors do not contribute to the output total. If no valid contributor remains for an output cell at a given time, the output cell is blank.

## Timeseries outputs

Primary output:

```text
<selected_output>.csv
```

Report:

```text
<selected_output_stem>_report.log
```

Optional spatial relationships:

```text
<selected_output_stem>_spatial_weights.csv
```

The spatial-weights table includes input/output UIDs, intersection area, polygon areas, output-area share, and input-area share.

**Current scope:** the Timeseries version implements area-based intensive and extensive transfer. The alternative-weighting system available in Base/Multifile mode is not currently implemented for temporal matrices.

---

# Existing intersection layers

Using an existing intersection layer can avoid repeated geometric overlay when the same grids are processed repeatedly.

The layer must provide fields identifying:

- input polygon UID;
- output polygon UID;
- optionally, intersection area in square metres.

If no intersection-area field is selected, area is computed from the intersection geometry in the configured processing CRS.

In Timeseries mode, multiple features representing the same input/output pair are aggregated by area before transfer.

---

# Reproducibility and quality control

For reproducible analyses:

- preserve source grid versions and CRS information;
- use stable UID fields;
- document whether area was read from an attribute or calculated from geometry;
- retain the generated warning/report logs;
- retain `grid_intersections` or the exported spatial-weights table when auditing transfer coefficients is important;
- use the same software version for repeated production runs.

The application reports or stops on a range of conditions including missing fields, non-unique UIDs, invalid/non-positive areas, missing external weights, and zero alternative-weight denominators.

---

# Troubleshooting

## `A usable PyQGIS installation could not be discovered`

Use a Python environment supplied/configured by QGIS, or run:

```bash
python main.py --qgis-prefix "<QGIS_PREFIX_OR_INSTALLATION_ROOT>"
```

Alternatively set `GRIDREPROJECTOR_QGIS_PREFIX`.

## `No module named qgis` or DLL/import errors

The Python interpreter is probably not compatible with the QGIS installation. Use the Python interpreter bundled with that QGIS installation rather than an unrelated system Python environment.

## A GeoPackage opens the wrong layer

Re-select the file. When multiple polygon layers are present, Grid Reprojector presents a polygon-layer chooser. In Multifile mode, corresponding GeoPackages should use the same selected layer name.

## Shapefile output has shortened field names

This is a Shapefile/DBF format limitation. Use GeoPackage for unrestricted practical field naming.

## Intersection fails on a polygon

Validate the source geometries in QGIS. Grid Reprojector does not automatically call `makeValid()` because geometry repair can alter polygon structure and should remain an explicit preprocessing decision.

## A temporary GeoPackage remains after processing

On Windows, GDAL/OGR can occasionally retain a file handle briefly. The application attempts repeated cleanup and treats a residual temporary file as non-fatal after successful output creation. It can be removed after the application is closed.

---

# Project files

- `main.py`: application entry point and three-page interface.
- `qgis_runtime.py`: portable PyQGIS runtime discovery and environment configuration.
- `base_reprojector.py`: Base/Multifile spatial transfer engine and GUI components.
- `timeseries_reprojector.py`: temporal matrix transfer engine and GUI components.
- `requirements.txt`: external software requirements.
- `README.md`: installation, methodology, and use instructions.
- `LICENSE.txt`: software license.
- `CITATION.cff`: software citation metadata.
- `.gitignore`: local development-file exclusions.
---

# Citation

For research and academic use, cite **Grid Reprojector v1.0.0** using the author and software metadata provided in [`CITATION.cff`](CITATION.cff). The release-specific Zenodo DOI can be added to the citation metadata once assigned.

# License

Grid Reprojector is distributed under the terms specified in [`LICENSE.txt`](LICENSE.txt).
