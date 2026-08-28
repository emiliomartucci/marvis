# Public release preflight evidence — 2026-08-28

This is a dated evidence snapshot, not a substitute for the live preflight that
must run at the exact merged release-source SHA immediately before tagging.

| Identity | Git tag | GitHub Release | PyPI | Evidence |
| --- | --- | --- | --- | --- |
| `0.3.8` | historical | historical | latest stable | PyPI project JSON reported `0.3.8` as the latest version. |
| `0.4.0` | `22b943c761e75764cf4bca75bdbbdfbfbc2863fa` | final record, zero assets | absent | Actions run `27822933381`: build succeeded; the publish job ran zero steps and failed after the approval wait expired. The identity is burned and must not be reused. |
| `0.4.1` | absent | absent | absent | Candidate namespace was unused across GitHub refs, GitHub Releases and the version-specific PyPI endpoint. |

The GitHub `pypi` environment readback showed:

- required reviewer: user `emiliomartucci`;
- `prevent_self_review=false`, so approval is an owner pause rather than
  independent review;
- `can_admins_bypass=false`;
- no deployment-branch policy.

Two external gates remain deliberately open in the source policy:

- owner readback of the exact PyPI Trusted Publisher coordinates;
- a ready approval-watchdog receipt for the bounded 24-hour window.

No tag, release, upload, publication or workflow dispatch was created while
capturing this snapshot.
