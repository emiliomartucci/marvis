# Local GUI — owned by `marvis`

The graphical interface of the local Marvis product. This directory is its
source of truth: the wheel ships an export built from here, the local API
serves it, and `marvis console` opens it.

## Perimeter

`surfaces.yaml` declares the routes this product owns. It is not documentation:
`scripts/validate_local_surfaces.py` reads the navigation table out of
`src/components/AppShell.tsx` and fails CI when the declaration and the code
disagree, or when a route outside the perimeter appears here.

Surfaces belonging to other products — the personal terminal Console, hosted
multi-user administration, SaaS-only pages — are not part of this source and
must not return. Before the U7 move they travelled inside the local release and
answered a direct URL even though navigation hid them.

## Build

```bash
npm ci
NEXT_PUBLIC_LOCAL_MODE=1 NEXT_PUBLIC_API_URL="" npm run build
```

The static export lands in `out/`. The release workflow copies it into
`core/api/console_dist/`, which is what the wheel carries.

## Receiving a desktop shell

`contracts/desktop-host.yaml` (repository root) defines what a desktop shell may
ask of the local runtime: the loopback endpoint, the capabilities it may drive,
what it must never do, and where permissions live. **No shell technology has
been chosen** — `docs/decisions/desktop-shell-selection.md` records the open
decision and the criteria it must answer.

The browser launcher stays the compatibility path: a shell drives the documented
capabilities, it does not reimplement them.
