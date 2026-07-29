"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function Home() {
  const router = useRouter();
  useEffect(() => {
    // Both targets must exist in this product's perimeter
    // (apps/desktop-ui/surfaces.yaml). /brain/diario/ and /terminal/ belong to
    // other surfaces and are not shipped here: redirecting to them left every
    // launch on a page absent from the export.
    router.replace("/diario/");
  }, [router]);
  return null;
}
