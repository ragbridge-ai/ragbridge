"""Synthetic PDFs for testing text extraction. Every name, company, city and sentence is invented.

Built inside the tests with fpdf2, never committed as binaries. The two-column CV
has a narrow side column (date range and city) beside a wide main column (company
and bullets), the layout that mixes columns when text is read in the order it was
written into the file.
"""

from fpdf import FPDF

CV_NAME = "Sam Sample"
CV_HEADER_CITY = "Westfield"
CV_JOBS = [
    (
        "Company A - Senior Engineer",
        "Mar 2022 - present",
        "Northtown",
        [
            "Designed a billing service that processed monthly invoices for enterprise"
            " customers and reconciled every payment automatically.",
            "Reduced report generation time from minutes to seconds by moving heavy"
            " analytical queries to dedicated read replicas.",
            "Mentored four junior engineers and organised weekly code reviews that"
            " improved the quality of every release.",
        ],
    ),
    (
        "Company B - Software Engineer",
        "Jan 2019 - Feb 2022",
        "Southville",
        [
            "Built a search feature for the product catalogue using full-text indexes,"
            " which doubled the share of visitors who found an item.",
            "Migrated the deployment pipeline from manual shell scripts to containers,"
            " so a new environment took minutes instead of days.",
            "Wrote integration tests for the checkout flow and fixed the flaky ones"
            " that used to block the release train.",
        ],
    ),
    (
        "Company C - Junior Developer",
        "Jun 2016 - Dec 2018",
        "Eastport",
        [
            "Maintained an internal inventory tool that warehouse staff used every day"
            " to track incoming and outgoing shipments.",
            "Fixed reported defects in a customer portal within the agreed deadlines"
            " and documented the root cause of each one.",
            "Wrote documentation that helped onboard new team members during their"
            " first two weeks on the project.",
        ],
    ),
    (
        "Company D - Intern",
        "Jul 2015 - May 2016",
        "Westport",
        [
            "Prototyped a small reporting dashboard for the finance team using"
            " spreadsheet exports and a simple web page.",
            "Cleaned and merged customer records from two legacy systems while keeping"
            " a log of every change made.",
            "Presented the results to the department head and collected feedback for"
            " the next iteration of the prototype.",
        ],
    ),
]

ARTICLE_SENTENCES = [
    f"The invented finding number {n} shows that sample {n} behaves as expected"
    f" under condition {n}."
    for n in range(1, 25)
]

ROTATED_NOTE = "ROTATED-MARGIN-NOTE"

_SIDE_X, _SIDE_W, _MAIN_X, _MAIN_W = 12, 40, 58, 140


def build_two_column_cv_pdf(stream_order: str = "main_first") -> bytes:
    """A one-page CV with a side column of dates and cities beside a main column.

    ``stream_order`` is which column is written into the file first for each job:
    ``main_first`` or ``side_first``. Text extractors that read in file order return
    different text for the two, from the same page.
    """
    pdf = FPDF()
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_xy(_MAIN_X, 14)
    pdf.cell(text=CV_NAME)
    pdf.set_font("Helvetica", size=10)
    pdf.set_xy(_MAIN_X, 24)
    pdf.cell(text=CV_HEADER_CITY)
    pdf.set_xy(_MAIN_X, 34)
    pdf.cell(text="https://example.com/sam-sample")
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_xy(_SIDE_X, 50)
    pdf.cell(text="Experience")

    y = 62.0
    for heading, dates, city, bullets in CV_JOBS:

        def main_block(
            top: float = y, heading: str = heading, bullets: list[str] = bullets
        ) -> float:
            pdf.set_xy(_MAIN_X, top)
            pdf.set_font("Helvetica", "B", 11)
            pdf.multi_cell(_MAIN_W, 6, heading, new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", size=10)
            for bullet in bullets:
                pdf.set_x(_MAIN_X)
                pdf.multi_cell(_MAIN_W, 5.2, "- " + bullet, new_x="LMARGIN", new_y="NEXT")
            return float(pdf.get_y())

        def side_block(top: float = y, dates: str = dates, city: str = city) -> None:
            pdf.set_font("Helvetica", size=9)
            pdf.set_xy(_SIDE_X, top)
            pdf.multi_cell(_SIDE_W, 5, dates, new_x="LMARGIN", new_y="NEXT")
            pdf.set_x(_SIDE_X)
            pdf.multi_cell(_SIDE_W, 5, city, new_x="LMARGIN", new_y="NEXT")

        if stream_order == "main_first":
            y_end = main_block()
            side_block()
        else:
            side_block()
            y_end = main_block()
        y = y_end + 8
    return bytes(pdf.output())


def build_two_column_article_pdf() -> bytes:
    """A page of two full-text columns, the left one written first (column by column)."""
    pdf = FPDF()
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    half = len(ARTICLE_SENTENCES) // 2
    for x, block in ((15, ARTICLE_SENTENCES[:half]), (110, ARTICLE_SENTENCES[half:])):
        pdf.set_xy(x, 20)
        pdf.multi_cell(85, 5, " ".join(block), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


SINGLE_COLUMN_KINDS = ("one line per paragraph", "wrapped paragraphs", "heading and list")


def build_single_column_pdf(kind: str) -> bytes:
    """A simple one-column page: the kind of PDF that must keep extracting as it always did."""
    pdf = FPDF()
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    if kind == "one line per paragraph":
        for text in ("First paragraph.", "Second paragraph.", "Third paragraph."):
            pdf.cell(text=text, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(4)
    elif kind == "wrapped paragraphs":
        paragraph = "This is a longer paragraph that wraps over several lines in one column. " * 3
        for _ in range(3):
            pdf.multi_cell(0, 6, paragraph.strip(), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(6)
    else:
        pdf.multi_cell(0, 6, "Heading", new_x="LMARGIN", new_y="NEXT")
        for item in ("first item in a list", "second item in a list", "third item in a list"):
            pdf.set_x(20)
            pdf.multi_cell(0, 6, "- " + item, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def build_pdf_with_rotated_note() -> bytes:
    """Body text plus a note rotated 90 degrees in the margin."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.set_xy(20, 30)
    pdf.multi_cell(
        150, 6, "Ordinary body text sits on the page as usual.", new_x="LMARGIN", new_y="NEXT"
    )
    with pdf.rotation(90, x=15, y=200):
        pdf.set_xy(15, 200)
        pdf.cell(text=ROTATED_NOTE)
    return bytes(pdf.output())
