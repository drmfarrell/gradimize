"""Shared Excel helpers: CSV-to-worksheet with the standard header styling.

One home for the header font/fill and column autosizing that the schedule
and finals exporters used to duplicate (and let drift).
"""

import csv
from io import StringIO


def write_csv_sheet(ws, csv_content: str, autosize_scan_rows: int = 200) -> None:
    """Fill a worksheet from CSV text with styled headers and sized columns.

    Args:
        ws: openpyxl worksheet.
        csv_content: CSV text; first row becomes the styled header.
        autosize_scan_rows: How many rows to scan when sizing columns.
    """
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    header_font = Font(bold=True)
    header_fill = PatternFill(
        start_color="D9E1F2", end_color="D9E1F2", fill_type="solid"
    )

    for row_idx, row in enumerate(csv.reader(StringIO(csv_content)), 1):
        for col_idx, value in enumerate(row, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            if row_idx == 1:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center")

    for col in range(1, ws.max_column + 1):
        max_len = 0
        col_letter = get_column_letter(col)
        for row in range(1, min(ws.max_row + 1, autosize_scan_rows)):
            val = ws.cell(row=row, column=col).value
            if val:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[col_letter].width = min(max_len + 2, 50)
