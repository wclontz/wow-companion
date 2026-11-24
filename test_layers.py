"""Layers in a square (2026-11-20): every layer has its own room in a square, and the level shown is the one
the minimap is showing (its view), falling back to where you stand.

    python test_layers.py
"""
import shutil
import tempfile

from quadmap import QuadMap, PER_SQ
from worldmap import WorldMap


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, wanted {want!r}"))
    return ok


def main():
    root = tempfile.mkdtemp(prefix="layers_")
    ok = True
    try:
        w = WorldMap(root)
        m = w.meta("T")
        # an outdoor run (layer 0, 0.78 px/yd) with a full square at (0..40 px), and an indoor run (layer 1)
        m["runs"] = {"out": {"k": 0.78, "layer": 0, "n": 9}, "in": {"k": 1.395, "layer": 1, "n": 9},
                     "new_in": {"k": 1.395, "layer": None, "n": 1}, "new_out": {"k": 0.78, "layer": None, "n": 1}}
        m["clips"] = [{"id": i + 1, "x": 10 + i, "y": 12, "run": "out", "file": f"c{i}.png", "mapping": True}
                      for i in range(PER_SQ)]
        far = {"id": 90, "x": 2, "y": 2, "mapping": True}          # worse centred than every kept shot
        ok &= check("full square turns away a worse outdoor shot", w._worth_keeping(m, dict(far, run="out"), m["runs"]["out"]), False)
        ok &= check("... but keeps the indoor view of the same square", w._worth_keeping(m, dict(far, run="in"), m["runs"]["in"]), True)
        ok &= check("... also while that run's layer is undecided", w._worth_keeping(m, dict(far, run="new_in"), m["runs"]["new_in"]), True)
        ok &= check("an undecided outdoor run shares the outdoor room", w._worth_keeping(m, dict(far, run="new_out"), m["runs"]["new_out"]), False)
        ok &= check("same room: same layer", QuadMap._same_room({"layer": 1, "k": 1.4}, {"layer": 1, "k": 0.8}), True)
        ok &= check("same room: other layer", QuadMap._same_room({"layer": 2, "k": 1.4}, {"layer": 1, "k": 1.4}), False)
        ok &= check("same room: nothing known", QuadMap._same_room({}, {"layer": 1}), True)

        # which level is shown: shots of three levels around (0, 0); level 2 is 300 yd away
        m["clips"] = ([{"id": 1, "x": 0, "y": 5, "run": "out", "file": "a.png", "mapping": True},
                       {"id": 2, "x": 3, "y": 0, "run": "in", "file": "b.png", "mapping": True},
                       {"id": 3, "x": 300, "y": 0, "run": "up", "file": "c.png", "mapping": True}])
        m["runs"]["up"] = {"k": 1.395, "layer": 2, "n": 9}
        m["tiles"] = {"0": {}, "1": {}, "2": {}}
        ok &= check("outdoor view -> the outdoor map", w.floor_for_view("T", [0, 0], "out"), 0)
        ok &= check("indoor view -> the indoor level here", w.floor_for_view("T", [0, 0], "in"), 1)
        ok &= check("indoor view near the upper level -> that level", w.floor_for_view("T", [298, 0], "in"), 2)
        ok &= check("indoor view, nothing near: the level it was on", w.floor_for_view("T", [150, 150], "in", cur=2), 2)
        w.cur_layer["T"] = 2
        ok &= check("indoor view, nothing near: the last indoor stretch's level", w.floor_for_view("T", [150, 150], "in"), 2)
        ok &= check("no view measured: no answer (position decides)", w.floor_for_view("T", [0, 0], None), None)
        # standing right on an indoor shot, with the outdoor one 5 yd off: position says indoor, the view wins
        m["draw_k"] = 1.395
        ok &= check("page: by position", w.info_grid("T", [3, 0])["floor"], 1)
        ok &= check("page: the minimap shows outdoors", w.info_grid("T", [3, 0], None, "out")["floor"], 0)
        ok &= check("page: the minimap shows indoors", w.info_grid("T", [0, 5], None, "in")["floor"], 1)
        m["tiles"] = {"0": {}}
        w._floor_here = {}
        ok &= check("indoor view but no indoor map yet: falls back", w.info_grid("T", [0, 5], None, "in")["floor"], 0)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    # a zoomed-in minimap (the add-on's zoom step is not 0) can't be measured into a view: an outdoor minimap a
    # few steps in slides like the indoor one. The view for the pages is then the add-on's indoors flag.
    import grids
    g = grids.Grids(tempfile.mkdtemp(prefix="zoomview_"))
    for zoom, indoors, before, want in ((3, False, "in", "out"), (2, True, "out", "in"), (0, False, "in", "in")):
        c = {"zoom": zoom, "indoors": indoors, "view": before, "win": []}
        got = g._views(c)
        if got != [] or c["view"] != want:
            print("zoomed-in view:", zoom, indoors, before, "->", c["view"], "wanted", want)
            ok = False
    print("ALL PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
