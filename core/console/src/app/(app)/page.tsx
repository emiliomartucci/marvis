"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

function isLocalMode(): boolean {
  const value = process.env.NEXT_PUBLIC_LOCAL_MODE;
  return value === "1" || value === "true";
}

export default function Home() {
  const router = useRouter();
  useEffect(() => {
    router.replace(isLocalMode() ? "/brain/diario/" : "/terminal/");
  }, [router]);
  return null;
}
