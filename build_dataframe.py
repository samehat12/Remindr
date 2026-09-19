from pathlib import Path

import pandas as pd


def format_column_names(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Replace underscores with spaces in column names."""
    renamed = dataframe.copy()
    renamed.columns = [column.replace("_", " ") for column in renamed.columns]
    return renamed


def find_numeric_columns(dataframe: pd.DataFrame) -> list[str]:
    """Return columns whose non-null values are all numeric."""
    numeric_columns: list[str] = []

    for column in dataframe.columns:
        non_null_values = dataframe[column].dropna()
        if non_null_values.empty:
            continue

        coerced_values = pd.to_numeric(non_null_values, errors="coerce")
        if coerced_values.notna().all():
            numeric_columns.append(column)

    return numeric_columns


def build_synthetic_dataframe(data_dir: str | Path | None = None) -> pd.DataFrame:
    """Combine every CSV in the synthetic_data folder into one dataframe."""
    base_dir = Path(data_dir) if data_dir is not None else Path(__file__).resolve().parent / "synthetic_data"
    csv_files = sorted(base_dir.glob("*.csv"))

    if not csv_files:
        raise FileNotFoundError(f"No CSV files found in {base_dir}")

    frames: list[pd.DataFrame] = []
    all_columns: list[str] = []

    for csv_file in csv_files:
        frame = pd.read_csv(csv_file)
        frames.append(frame)

        for column in frame.columns:
            if column not in all_columns:
                all_columns.append(column)

    combined = pd.concat(
        [frame.reindex(columns=all_columns) for frame in frames],
        ignore_index=True,
    )
    combined = combined.dropna(axis=1, how="all")
    combined = format_column_names(combined)

    return combined


if __name__ == "__main__":
    dataframe = build_synthetic_dataframe()
    print(dataframe.head())
    print(f"\nRows: {len(dataframe)}")
    print(f"Columns: {len(dataframe.columns)}")
    print(f"Numeric columns: {find_numeric_columns(dataframe)}")