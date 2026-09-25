"""Generate synthetic VRR reference PDFs into ./knowledge_uploads.

Feeds the knowledge path (docs/knowledge-flow.md): register → human approve →
chunk → PII-redact → embed → pgvector search. The documents are invented for the
demo — no real field, no real operating limits — but every number in them agrees
with the deterministic core (band 0.90–1.10, 15 percent clamp, extrapolated PVT
is suspect, three-stage approval) so a retrieved passage never contradicts a
computed one.

Writes the PDFs by hand rather than pulling in reportlab: one Helvetica text
stream per page, plus an xref table. Keeps the repo dependency-free and offline.

    python scripts/make_sample_pdfs.py
    make knowledge          # register, then approve in knowledge_registry, then ingest
"""
from __future__ import annotations

import os
import textwrap

OUT_DIR = os.environ.get("VRR_KNOWLEDGE_DIR", "./knowledge_uploads")
WIDTH, LEADING, TOP, BOTTOM, LEFT, SIZE = 96, 14, 720, 72, 72, 10


# ---- the smallest PDF writer that pypdf can read back ------------------------
def _escape(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _paginate(lines: list[str]) -> list[list[str]]:
    per_page = int((TOP - BOTTOM) / LEADING)
    return [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[]]


def write_pdf(path: str, body: str) -> int:
    """Lay `body` out as Helvetica text pages and write a valid PDF. Returns pages."""
    lines: list[str] = []
    for para in body.strip().split("\n"):
        lines.extend(textwrap.wrap(para, WIDTH) or [""])
    pages = _paginate(lines)

    objs: list[bytes] = []                       # object 1..N, in order
    n_pages = len(pages)
    kids = " ".join(f"{4 + i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i in range(n_pages):                     # page objects, then their streams
        objs.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                     f"/Resources << /Font << /F1 3 0 R >> >> "
                     f"/Contents {4 + n_pages + i} 0 R >>").encode())
    for page in pages:
        text = "".join(f"({_escape(ln)}) Tj T*\n" for ln in page)
        stream = (f"BT /F1 {SIZE} Tf {LEADING} TL {LEFT} {TOP} Td\n{text}ET").encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body_bytes in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body_bytes + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1) + b"0000000000 65535 f \n"
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref_at))
    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return n_pages


# ---- the documents -----------------------------------------------------------
DOCS: dict[str, str] = {}

DOCS["vrr_pvt_data_standard.pdf"] = """
PVT Data Standard and Formation Volume Factors (SYNTHETIC DEMO DOCUMENT)

1. Purpose
This synthetic standard exists to exercise the knowledge-ingestion path of the
vrr-agent-open demo. It is not an operating document and describes no real field.

2. Why PVT sits underneath every VRR number
VRR compares injected reservoir volume with produced reservoir volume. Meters measure
surface volumes, so every barrel must be converted to reservoir conditions before the
ratio means anything. That conversion is done with formation volume factors taken from
PVT laboratory tests: Bo for oil, Bw for water, Bg for gas, and the solution gas oil
ratio Rs for the gas that comes out of solution. A VRR is therefore only ever as
trustworthy as the PVT behind it.

3. Reservoir volume conversion
Produced reservoir volume is oil volume times Bo, plus water volume times Bw, plus the
free gas volume times Bg, where free gas is total produced gas less the solution gas
implied by Rs. Injected reservoir volume is injected water times Bw at the injection
interval conditions. Both sides are evaluated at the prevailing pattern pressure for
the period, not at the initial reservoir pressure.

4. Lookup rule: exact, closest, extrapolated
Formation volume factors are looked up by completion, test date and pressure. Three
outcomes are possible and each is recorded on the row so it can be audited later.
An exact lookup means a laboratory test exists at that pressure for that completion.
A closest lookup means the nearest measured test pressure was used and the difference
is small enough to be accepted. An extrapolated lookup means the pattern pressure lies
outside the range of every measured test, so the factor was projected beyond the data.

5. Extrapolated PVT is suspect by default
An extrapolated factor carries unquantified error. The derived reservoir volumes, and
therefore the VRR built from them, inherit that error. Any period flagged as
extrapolated is treated as a data-quality finding first and a reservoir signal second.
Investigate the source data before proposing any injection change on that period. A
change proposed on an extrapolated period is escalated to the data steward rather than
executed, because the apparent anomaly may be an artefact of the lookup and not a
movement in the reservoir at all.

6. Test coverage expectations
Every completion allocated to a pattern should carry at least one PVT test. The tested
pressure range should bracket the operating pressure range of the pattern over the
reporting period. Where a pattern has drifted outside its tested range, schedule a new
sample rather than continuing to extrapolate.

7. Amount type
Each volume row derives an amount type from its stream and direction: produced oil,
produced water, produced gas, or injected water. The amount type selects which factor
applies. A row with no resolvable amount type is excluded by the reporting gate rather
than silently defaulted.
"""

DOCS["vrr_pattern_allocation_standard.pdf"] = """
Pattern Allocation and Data Quality Standard (SYNTHETIC DEMO DOCUMENT)

1. Purpose
This synthetic standard exists to exercise the knowledge-ingestion path of the
vrr-agent-open demo. It is not an operating document and describes no real field.

2. Completions, not wells
Volumes are keyed by completion, never by well. A well may have several completions in
different intervals and each can belong to a different pattern. The completion is the
smallest unit that can be allocated, metered and clamped, so it is the unit of record
throughout.

3. Many-to-many allocation
A completion may contribute to more than one pattern and a pattern is built from many
completions. The link carries a pattern contribution factor between zero and one, valid
over a date window. The sum of a completion's contribution factors across all patterns
on any given date must not exceed one. A sum above one means the same barrel has been
counted twice and the period is rejected.

4. Roles
Each completion carries a role in the pattern: injector, producer, or both where an
interval has been converted during the window. Injected volumes from producers and
produced volumes from injectors are treated as data errors, not as reservoir behaviour.

5. Time windows
Contribution factors and pattern pressure are both time-windowed. A completion that
joined a pattern in March contributes nothing to that pattern in February. Every
allocation query must be evaluated against the date of the volume row, not against the
current configuration, or history silently rewrites itself every time the model changes.

6. Reporting gate
A pattern month is published only when it has volumes on both sides of the ratio, a
pattern pressure for the period, and PVT coverage for every allocated completion.
Months that fail the gate are held back rather than published with a partial
denominator, because a VRR computed from half a pattern looks like a reservoir event.

7. Data quality checks
The ingestion job runs four deterministic checks and records every finding: allocation
factors summing above one, volumes whose completion belongs to no pattern, patterns
with no pressure for the period, and allocated completions with no PVT test. All four
are input problems. None of them are reservoir problems, and none should ever reach an
analyst dressed as one.

8. Daily and monthly rollup
Daily pattern VRR is aggregated to monthly with volume-weighted average formation
volume factors, so a high-rate day carries more weight than a low-rate day. Cumulative
VRR is the running sum of reservoir volumes from first injection, not the average of
the monthly ratios.
"""

DOCS["vrr_injection_change_procedure.pdf"] = """
Injection Change Approval and Response Calibration Procedure (SYNTHETIC DEMO DOCUMENT)

1. Purpose
This synthetic procedure exists to exercise the knowledge-ingestion path of the
vrr-agent-open demo. It is not an operating document and describes no real field.
Document owner: Field Operations Data Steward, ops-steward@example.com, desk
(555) 214-7788. Contact details in this paragraph are fictitious and exist to
demonstrate that personally identifying information is redacted before embedding.

2. Target band
A pattern is on target when its VRR sits between 0.90 and 1.10. Sustained VRR above
1.10 indicates over-injection: pressure builds above the datum, injected water cycles
through high-permeability streaks, and injection cost is spent without incremental oil.
Sustained VRR below 0.90 indicates under-injection: pressure declines toward the bubble
point, solution gas comes out of solution, and relative permeability to oil falls.

3. Safety clamps on any proposed change
No single valve adjustment may change an injector's surface rate by more than 15
percent. Injection pressure must stay below the completion's maximum allowable
injection pressure and below the fracture gradient of the interval. Where the physics
asks for more than 15 percent, the change is clamped to 15 percent and the remainder is
carried into the next review cycle rather than taken in one step.

4. Response factor
VRR response to an injection change is rarely one to one. Patterns with strong aquifer
support respond less; confined patterns respond more. Each pattern carries a learned
response factor, rho, and the recommended change is divided by it. After a change has
been executed and the next month has been built, the observed response is compared with
the predicted response and rho is updated by an exponentially weighted moving average.
A single surprising month therefore nudges rho rather than replacing it.

5. Precedent
Before a recommendation is issued, the most recent executed adjustment on the same
pattern with the same driver is retrieved and shown alongside it. An analyst reading a
recommendation should be able to see what was tried last time and what happened.

6. Input audit gate
Every candidate period is audited before any recommendation is drafted. A period
classified as a data artefact is routed to the data steward and no valve change may be
proposed on it. Only a period classified as a real signal may carry a recommendation.
An inconclusive verdict is treated as a data artefact until resolved.

7. Approval chain
Recommendations are advisory. A draft is reviewed by the pattern analyst, approved by
the reservoir manager, and accepted by the site operator before any valve moves. Each
stage is recorded with who acted and when. Nothing in the system executes a change
against field equipment.

8. What is recorded
Every executed adjustment is written to the adjustment history with the pattern, the
date, the driver, the recommended and executed change, the approver at each stage, and
once the following month has been built, the actual post-change VRR. That last column
is what makes the response factor learnable rather than assumed.
"""

if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, body in DOCS.items():
        pages = write_pdf(os.path.join(OUT_DIR, name), body)
        print(f"wrote {name}  ({pages} page(s))")
    print(f"\n{len(DOCS)} PDF(s) in {OUT_DIR} — now run `make knowledge` to register "
          "them, approve each row in vrr_agent.knowledge_registry, then `make "
          "knowledge` again to chunk, redact, embed and index.")
