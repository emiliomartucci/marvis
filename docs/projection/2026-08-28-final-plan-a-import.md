# Final Plan A projection import — `d413f09b`

This receipt records the exact public/shared payload consumed by Plan B. It
does not claim a merge, package publication, release, deploy, or live runtime.

## Exact provenance

| Coordinate | Value |
| --- | --- |
| Plan A merge/source SHA | `d413f09bf0e43c1929d1c774c77c7eed1d56bf18` |
| Exporter SHA | `d413f09bf0e43c1929d1c774c77c7eed1d56bf18` |
| Exporter identity SHA-256 | `19bc0c8cc1cbf1946d099a696ad6b5cb07e5399ea72ffeac04fc55633536b7e1` |
| Payload manifest SHA-256 | `9c4d0928f894f4c3dfa73be9757d3a1d607fadb90388e3a4b67f99a416d82003` |
| Payload SHA-256 | `07069a51ce4ddb70731d6d2838eb50018538d059548ab5691fb344e5a2788f49` |
| Payload file-tree SHA-256 | `463db6e0868a47322c422404a2a50de9e7995206ef6d2f61c85698afbeb68486` |
| Payload mode-tree SHA-256 | `25415e9bb0b6cbbb72f2052ed6f09881fd2d4153d573113d27257040cd6cd582` |
| OSS consumer manifest SHA-256 | `7e5f513d12c6f7406b6cd24909f0e4ddf465cf08b712751208c5a4b9f22908b8` |
| OSS import base | `cb7cda35e9ec455f794a5ce6c7f59d94dfb17f99` |

Two exports from the exact merge SHA were byte-identical. Two repository-local
dry runs were also byte-identical, both with report SHA-256
`2ff5e531aa74a33957940ad2de76d4c20520a8398ffd9736060f82338cef57af`.

## Apply evidence

The fail-closed importer accepted 1,198 importable files from a 1,202-file
payload. The local ownership map took precedence over the consumer manifest
and preserved four OSS-owned root files. The importer found zero blocked files
and zero violations, and classified 1,183 files as already synchronized plus
15 exact upstream replacements. The committed machine-readable report is
`docs/projection/2026-08-28-final-import-report.json`, SHA-256
`cdc4abc2c811632495e7dae1e425c639a3cc1f55b10367ef545857eaab1c89e6`.

The transactional backup contained 1,198 entries. Its manifest SHA-256 was
`9909a18cadae5ccef296990802abc9b20fd3950523205c6fd39be30a83678155`;
the pre-apply tree was
`9912dea8dc05cb9efabd128a90a80fd280860431f1dfb2d033a827fe454f986e`
and the imported-file readback digest was
`b3455cbeb1b254cb1c814407d84d9ed8ccb4e85244459f989ee70ba3a684aedc`.

## Consumer reconciliation

After the exact import, Plan B reapplied four explicit downstream adaptations
over shared payload paths: Python 3.10 claim validation in the install workflow,
cross-platform file locking for Windows, and revision-pinned Granite offline
diagnostics. All other changed shared files remain byte-identical to the Plan A
payload. Compatibility fixtures were rebuilt from the exact source SHA; the
OpenAPI bytes remained unchanged.

The apply report binds ownership-map SHA-256
`3942f10ccbfc65fbb0361b8318a08f6d5e0774a74a20ed841e17c7c141598c01`.
After apply, only its provenance comment changed from the pre-final candidate
to `d413f09b`; policy bytes and version 2 semantics are unchanged. The resulting
file SHA-256 is
`a9237ec70b8c25114a59037b382faa3db4df8cc8e8eff015f9fcc0bb867e6ccf`.

The external importer backup proves transactional apply and immediate
readback. Once the four downstream adaptations are present, rollback is the
normal Git revert of this projection commit; the importer backup is not a
post-reconciliation rollback mechanism because it deliberately rejects drift.

The historical pre-candidate receipt remains unchanged and is not current
import evidence.
