"""Media intake, RealityScan orchestration, and georeferencing.

This package is the reconstruction half of WorldForge. It has two entry points:

    reconstruction/pipeline.py      the operator CLI — intake, orient, prepare,
                                    build, anchor, package
    `anchor_model`                  the georeferencing library entry point, used
                                    by the CLI and by anything else that has a
                                    scan and a coordinate

Everything here is import-safe on a machine with no RealityScan, no PyAV and no
HEIC support: the heavy and optional dependencies are imported inside the
functions that need them, and a function that cannot run raises rather than
returning a plausible-looking guess. `app.pipeline` catches that and falls back
to the prepared asset, which is the behaviour the live demo depends on.

Three names `app.pipeline` probes for are deliberately *absent* here:

    analyse_media      `app.pipeline.analyse_media` already reads EXIF GPS and
                       catches burst-mode near-duplicates as well as blur, so a
                       narrower version here would be a downgrade, not a hook.
    reconstruct        would have to run RealityScan, a Windows desktop
                       application that requires a human to isolate the subject
                       between alignment and meshing (`pipeline.py prepare`, then
                       `pipeline.py build`). Driving that from an HTTP request
                       means either blocking for many minutes or reporting a scan
                       as finished when it is not.
    coverage_report    would need the camera poses from the RealityScan project,
                       which the current export does not include.

Defining them as stubs would make the capability probe light up green for things
that never work. Leaving them undefined makes the app say, on screen, that
reconstruction is not wired on this machine — which is true, and is the whole
reason the prepared asset path exists.
"""

from __future__ import annotations


__all__ = ["anchor_model"]


def anchor_model(*args, **kwargs):
    """Anchor a scan to the Earth. See `reconstruction.anchor.anchor_model`."""
    from .anchor import anchor_model as run

    return run(*args, **kwargs)
