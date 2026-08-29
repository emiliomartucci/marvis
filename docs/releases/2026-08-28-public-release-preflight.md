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

Two external gates remain deliberately open outside the source policy:

- owner readback of the exact PyPI Trusted Publisher coordinates;
- a ready approval-watchdog receipt for the bounded 24-hour window.

The candidate cannot assert either fact about itself. Fresh signed JSON
receipts must be provided through the repository Actions variables
`MARVIS_PYPI_TRUSTED_PUBLISHER_RECEIPT` and
`MARVIS_APPROVAL_WATCHDOG_RECEIPT`; the source policy contains only their
schema and expected coordinates. The tag path also requires a successful,
fresh `workflow_dispatch` preflight for the exact release SHA and binds that
run into the artifact manifest.

The projected shared source is MarvisX PR `#314`, candidate
`6c25bebde4d8feee6455994aeca37afc41da4a78`, merge
`962e0fa6bb15ca71e3b20e9c99636aa93c631271`, cross-checked against
`contracts/engine-pin.yaml` and the live GitHub PR readback.

No tag, release, upload, publication or workflow dispatch was created while
capturing this snapshot.
