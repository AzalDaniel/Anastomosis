# RULES_CANDIDATES — worker W1 (deliver/archive, deliver/bundle, _shared, router, render_index, __init__)

One sentence each, with `file:line` from the pre-sweep source. Orchestrator adjudicates.

- Route preference is fixed cheapest-first: vendor API > C-CDA import > browser automation (`src/anastomosis/deliver/router.py:5-7`).
- A delivered-name collision between two different source ids raises `DeliveredNameCollision` rather than silently merging their output into one slot (`src/anastomosis/deliver/_shared.py:67-82`).

## Loose ends

(none found in this worker's files)
