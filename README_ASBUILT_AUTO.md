# TELETECH As-Built Auto Processor (CLI)

This is an **isolated command-line processor**. It does not replace the existing
TELETECH UI workflow and is not yet wired to a button in `server.py`.

## Install (Windows)

From the repository folder:

```powershell
py -3.11 -m pip install -r requirements-asbuilt.txt
```

## Run one floor at a time

Use the DXF for the same floor represented by the supplied PDF page(s):

```powershell
py -3.11 asbuilt_apply.py --dxf "Mansoura University - HUM - G - A.B.dxf" --pdf "AsBuilt علوم إنسانيه جامعه المنصوره - Copy - Copy_p1.pdf" "AsBuilt علوم إنسانيه جامعه المنصوره - Copy - Copy_p2.pdf" --out "exports\asbuilt_auto_ground"
```

The processor reads **every page** in every PDF argument. Do not pass PDFs from
different floors with one DXF. For a different floor, run the command again with
that floor's DXF and PDFs.

## Outputs

- `*_AsBuilt.dxf`: a new drawing; the source DXF is not overwritten.
- `asbuilt_report.json`: registration diagnostics, matched endpoints, applied
  changes, and items rejected for review.

## Markup behavior

- Pink and Red: normalized to Pink; delete the endpoint (Patch Panel action SHIFT).
- Yellow: delete the endpoint (Patch Panel action RESERVE).
- Orange: detect the blue ballpoint arrow and move the matched endpoint to the
  registered arrow tip; keep the Patch Panel slot.
- Green: no CAD change.

A change is only attempted after page registration and an unambiguous endpoint
match. Ambiguous matches, unconfirmed arrows, unsupported entities, or failed
nested edits are recorded for review.

## Important limitations

- This CLI does not yet update the TELETECH Patch Panel/label Excel outputs and
  does not currently have a UI button/API route in `server.py`.
- It targets the configured endpoint block names in `asbuilt_apply.py`.
- Nested static INSERT paths are cloned privately; dynamic, attributed, xref,
  anonymous, or unsupported block structures are not modified.
- Registration and matching must be verified on the actual project's floor
  drawing before using the generated DXF for construction.
- The repository CI tests synthetic DXF instance isolation and markup colors;
  they do not certify every entity in the Mansoura production DXF.
