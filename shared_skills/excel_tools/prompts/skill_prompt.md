These tools read workbooks in the `excel_inbox` workspace. They never change a file.

- `list_workbooks()`: find out which files exist. Call it first when the user has not named a file.
- `describe_workbook(file)`: sheets, columns, types and first rows. Call it before any query so you use real column names.
- `profile_sheet(file, sheet)`: blanks, distinct values, stats, duplicates, outliers per column.
- `query_sheet(file, sheet, filter, group_by, metrics, sort_by, limit)`: filtered rows or grouped totals.
- `pivot_sheet(file, index, columns, values, agg)`: a cross-table with totals.

If the first rows show the headers are not on row 0 (titles, blank rows above
the table), pass `header_row` with the right zero-based row.
Column names in a `filter` that contain spaces go in backticks.
If a tool returns `status: error`, read the message, fix the call once, and if
it still fails tell the user what failed.
