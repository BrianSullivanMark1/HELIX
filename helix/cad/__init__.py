"""helix.cad — the hologram compile worker (a subprocess; never imported by the app process).

build123d drags in the OCCT kernel (~2s import, hundreds of MB resident), so the app process never
imports it: the Build123dCad adapter spawns `helix.cad.runner` as a worker instead, the same
isolation the OpenSCAD CLI gave us for free. The worker reads one job file, computes the design,
writes every artifact (STL, STEP, 3MF, preview PNG, meta JSON), and reports one JSON result.
"""
