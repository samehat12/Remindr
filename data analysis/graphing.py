from __future__ import annotations

import pandas as pd
import plotly.express as px

from build_dataframe import build_synthetic_dataframe, find_numeric_columns


DATE_COLUMNS = [
    "date",
    "event date",
    "timestamp",
    "scheduled time",
    "reminder sent at",
    "response received at",
    "completion time",
]


def find_date_column(dataframe: pd.DataFrame, numeric_column: str) -> str:
    """Choose the best date-like column for the selected numeric column."""
    candidate_rows = dataframe[dataframe[numeric_column].notna()]

    best_column = None
    best_overlap = 0

    for column in DATE_COLUMNS:
        if column not in candidate_rows.columns:
            continue

        overlap = candidate_rows[column].notna().sum()
        if overlap > best_overlap:
            best_column = column
            best_overlap = overlap

    if best_column is None or best_overlap == 0:
        raise ValueError(f"No date column found for '{numeric_column}'.")

    return best_column


def plot_numeric_column(dataframe: pd.DataFrame, numeric_column: str) -> None:
    """Plot a numeric column against its matching date-like column."""
    date_column = find_date_column(dataframe, numeric_column)

    plot_frame = dataframe[[date_column, numeric_column]].copy()
    plot_frame = plot_frame.dropna(subset=[date_column, numeric_column])
    plot_frame[numeric_column] = pd.to_numeric(plot_frame[numeric_column], errors="coerce")
    plot_frame[date_column] = pd.to_datetime(plot_frame[date_column], errors="coerce")
    plot_frame = plot_frame.dropna(subset=[date_column, numeric_column]).sort_values(date_column)

    if plot_frame.empty:
        raise ValueError(f"No plottable rows found for '{numeric_column}'.")

    figure = px.line(
        plot_frame,
        x=date_column,
        y=numeric_column,
        markers=True,
        title=f"{numeric_column} vs {date_column}",
    )
    figure.show()


def prompt_for_column(numeric_columns: list[str]) -> str:
    """Prompt until the user enters an exact numeric column name."""
    print("Available numeric columns:")
    for column in numeric_columns:
        print(f" - {column}")

    while True:
        selected_column = input("\nEnter the exact column name to plot: ").strip()
        if selected_column in numeric_columns:
            return selected_column

        print("Column not found. Please enter one of the names exactly as shown.")


def main() -> None:
    dataframe = build_synthetic_dataframe()
    numeric_columns = find_numeric_columns(dataframe)
    selected_column = prompt_for_column(numeric_columns)
    plot_numeric_column(dataframe, selected_column)


if __name__ == "__main__":
    main()