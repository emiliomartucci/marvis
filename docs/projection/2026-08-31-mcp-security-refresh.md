# MCP security projection — `8dfd16e4`

This receipt records the controlled public/shared synchronization from MarvisX
PR #349 into the OSS consumer. It does not claim a release or publication.

## Exact source

| Coordinate | Value |
| --- | --- |
| MarvisX candidate | `77c865633b7447c4a778b2569f4ceec9a3b7dce6` |
| MarvisX merge | `8dfd16e4e275b69435e0348258daf86f67898997` |
| Payload SHA-256 | `d41411373ecaf584ee4406a23c9ba7fedc1915a9564f8734983d9a826cd70ae7` |
| Payload manifest SHA-256 | `d4f567d7b5ec7d34adaabfb41f16ddb5174cd8320bccd88dde262fd2b1c5c8b4` |
| OSS consumer manifest SHA-256 | `05ef05ad8f7d76b4c15221845ad52a8dc50b7bac2dae6e3265d4107d58a6a53c` |
| OSS import base | `bf868421caa20f356a519c4b48c3e1fa41df7e9e` |

Two dry runs were byte-identical. The accepted report SHA-256 was
`809ad6d32f44580e78ee6d8064a1838126607da8e1e04ad320dffeb3fc40c240`:
1,207 shared files were already synchronized, five OSS-owned files were
preserved, and no forbidden path or content was found.

The transactional apply completed with report SHA-256
`e08453c0a847e5271524be0f2ab42869e4eb8259f11fd8322e98e5b23fa44cac`.
It produced no shared-file diff because the projected security change touched
only consumer-owned root files. This commit therefore advances the exact
engine provenance, raises the OSS MCP floor to 1.28.1, rebuilds the unchanged
API fixtures against the new source SHA, and invalidates the burned 0.4.7
release candidate. A separate release-only change must bind the exact merge of
this projection before 0.4.8 can be built.
