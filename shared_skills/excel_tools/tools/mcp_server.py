"""Read-only Excel analysis tools shared by workbook_reader and data_expert."""
import argparse
import os
from pathlib import Path

import pandas as pd
from mcp.server.fastmcp import FastMCP

_parser = argparse.ArgumentParser()
_parser.add_argument("--workspace", default=".")
_args, _ = _parser.parse_known_args()   # the platform may pass more flags

# EXCEL_DIR is set from tools.mcp[].env; --workspace is the fallback.
ROOT = Path(os.environ.get("EXCEL_DIR") or _args.workspace).resolve()
SUFFIXES = {".xlsx", ".xlsm", ".csv"}
MAX_ROWS = 200          # cap on rows returned to the model per call
AGGS = {"sum", "mean", "median", "min", "max", "count", "nunique"}

mcp = FastMCP("ExcelTools")


def _resolve(file: str) -> Path:
    path = (ROOT / file).resolve()
    if ROOT not in path.parents and path != ROOT:
        raise ValueError("path is outside the workspace")
    if not path.is_file():
        raise FileNotFoundError(file)
    if path.suffix.lower() not in SUFFIXES:
        raise ValueError(f"unsupported file type {path.suffix}; use .xlsx, .xlsm or .csv")
    return path


def _load(file: str, sheet: str | None, header_row: int) -> pd.DataFrame:
    path = _resolve(file)
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path, header=header_row)
    else:
        df = pd.read_excel(path, sheet_name=sheet or 0, header=header_row, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    return df.dropna(how="all")


def _records(df: pd.DataFrame) -> list[dict]:
    out = df.head(MAX_ROWS).copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.strftime("%Y-%m-%d")
    return out.astype(object).where(out.notna(), None).to_dict(orient="records")


def _error(exc: Exception) -> dict:
    return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def list_workbooks() -> dict:
    """List the Excel and CSV files the user has uploaded."""
    try:
        files = [
            {"file": str(p.relative_to(ROOT)), "size_kb": round(p.stat().st_size / 1024, 1)}
            for p in sorted(ROOT.rglob("*"))
            if p.is_file() and p.suffix.lower() in SUFFIXES and not p.name.startswith("~$")
        ]
        return {"status": "ok", "files": files}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def describe_workbook(file: str) -> dict:
    """List every sheet in a workbook with its size, columns and first rows.

    Args:
        file: File name as returned by list_workbooks, e.g. "sales_2026.xlsx".
    """
    try:
        path = _resolve(file)
        if path.suffix.lower() == ".csv":
            frames = {"(csv)": _load(file, None, 0)}
        else:
            frames = pd.read_excel(path, sheet_name=None, engine="openpyxl")
        sheets = []
        for name, df in frames.items():
            df = df.dropna(how="all")
            sheets.append({
                "sheet": name,
                "rows": len(df),
                "columns": [{"name": str(c), "dtype": str(t)} for c, t in df.dtypes.items()],
                "first_rows": _records(df.head(5)),
            })
        return {"status": "ok", "file": file, "sheets": sheets}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def profile_sheet(file: str, sheet: str = "", header_row: int = 0) -> dict:
    """Profile data quality and statistics of one sheet.

    Returns per column: type, blanks, distinct values, numeric stats or top
    values, plus duplicate row count and numeric outliers (beyond 1.5 IQR).

    Args:
        file: File name as returned by list_workbooks.
        sheet: Sheet name; empty means the first sheet.
        header_row: Zero-based row that holds the column headers.
    """
    try:
        df = _load(file, sheet or None, header_row)
        cols = []
        for c in df.columns:
            s = df[c]
            info = {
                "column": c,
                "dtype": str(s.dtype),
                "blanks": int(s.isna().sum()),
                "distinct": int(s.nunique(dropna=True)),
            }
            if pd.api.types.is_numeric_dtype(s) and s.notna().any():
                q1, q3 = s.quantile(0.25), s.quantile(0.75)
                iqr = q3 - q1
                info.update({
                    "min": float(s.min()), "max": float(s.max()),
                    "mean": round(float(s.mean()), 4), "median": float(s.median()),
                    "sum": float(s.sum()),
                    "outliers": int(((s < q1 - 1.5 * iqr) | (s > q3 + 1.5 * iqr)).sum()),
                })
            elif pd.api.types.is_datetime64_any_dtype(s) and s.notna().any():
                info.update({"first": str(s.min().date()), "last": str(s.max().date())})
            else:
                info["top_values"] = {str(k): int(v) for k, v in s.value_counts().head(5).items()}
            cols.append(info)
        return {
            "status": "ok", "file": file, "sheet": sheet or "(first)",
            "rows": len(df), "duplicate_rows": int(df.duplicated().sum()), "columns": cols,
        }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def query_sheet(
    file: str,
    sheet: str = "",
    filter: str = "",
    group_by: list[str] | None = None,
    metrics: dict[str, str] | None = None,
    sort_by: str = "",
    descending: bool = True,
    limit: int = 50,
    header_row: int = 0,
) -> dict:
    """Filter, group, aggregate and sort rows of one sheet.

    Without group_by it returns matching rows; with group_by it returns one
    row per group with the requested metrics.

    Args:
        file: File name as returned by list_workbooks.
        sheet: Sheet name; empty means the first sheet.
        filter: pandas query expression, e.g. "Region == 'North' and Amount > 1000".
            Wrap column names with spaces in backticks: "`Order Date` >= '2026-01-01'".
        group_by: Columns to group by, e.g. ["Region", "Product"].
        metrics: Column to aggregation, e.g. {"Amount": "sum", "Order ID": "count"}.
            Allowed: sum, mean, median, min, max, count, nunique.
        sort_by: Column to sort the result by.
        descending: Sort largest first when true.
        limit: Maximum rows to return (at most 200).
        header_row: Zero-based row that holds the column headers.
    """
    try:
        df = _load(file, sheet or None, header_row)
        total = len(df)
        if filter:
            df = df.query(filter, engine="python")
        matched = len(df)
        if group_by:
            metrics = metrics or {group_by[0]: "count"}
            bad = set(metrics.values()) - AGGS
            if bad:
                return {"status": "error", "error": f"unsupported aggregation {sorted(bad)}"}
            df = df.groupby(group_by, dropna=False).agg(metrics).reset_index()
        elif metrics:
            df = df.agg(metrics).to_frame("value").reset_index(names="column")
        if sort_by:
            df = df.sort_values(sort_by, ascending=not descending)
        return {
            "status": "ok", "rows_in_sheet": total, "rows_matched": matched,
            "result_rows": len(df), "rows": _records(df.head(min(limit, MAX_ROWS))),
        }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def pivot_sheet(
    file: str,
    index: str,
    columns: str,
    values: str,
    agg: str = "sum",
    sheet: str = "",
    filter: str = "",
    header_row: int = 0,
) -> dict:
    """Build a pivot table: one row per index value, one column per columns value.

    Args:
        file: File name as returned by list_workbooks.
        index: Column whose values become rows, e.g. "Region".
        columns: Column whose values become columns, e.g. "Month".
        values: Numeric column to aggregate, e.g. "Amount".
        agg: One of sum, mean, median, min, max, count, nunique.
        sheet: Sheet name; empty means the first sheet.
        filter: Optional pandas query expression applied first.
        header_row: Zero-based row that holds the column headers.
    """
    try:
        if agg not in AGGS:
            return {"status": "error", "error": f"unsupported aggregation {agg}"}
        df = _load(file, sheet or None, header_row)
        if filter:
            df = df.query(filter, engine="python")
        # pivot_table drops rows with a blank key; keep them so totals match query_sheet
        df[[index, columns]] = df[[index, columns]].astype(object).fillna("(blank)")
        pt = pd.pivot_table(df, index=index, columns=columns, values=values,
                            aggfunc=agg, margins=True, margins_name="Total")
        pt.columns = [str(c) for c in pt.columns]
        return {"status": "ok", "rows": _records(pt.reset_index())}
    except Exception as exc:
        return _error(exc)


if __name__ == "__main__":
    mcp.run(transport="stdio")
