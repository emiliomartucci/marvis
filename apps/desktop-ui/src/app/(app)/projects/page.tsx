"use client";

import LocalProjectsSurface from "@/components/projects/local/LocalProjectsSurface";

// The page kept its own NEXT_PUBLIC_LOCAL_MODE branch after the shell lost its
// own: with the flag unset — plain `npm run dev`, and the CI job — /projects/
// rendered the hosted sidebar and a placeholder inside the local shell. This
// product has one projects surface.
export default function ProjectsPage() {
  return <LocalProjectsSurface />;
}
