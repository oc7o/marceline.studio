"use client";
/**
 * Ask the server for this page again every `intervalMs`, for as long as something is still
 * settling (ADR 0036).
 *
 * The overview's one moving part: on production an upload is accepted first and added a few
 * moments later (ADR 0028 write path), so while a submission is processing the page re-renders
 * itself on the server rather than the browser polling an API and patching a cache. It renders
 * nothing, holds no data and knows nothing about what it is waiting for.
 */
import { useRouter } from "next/navigation";
import { useEffect } from "react";

export function Refresher({ intervalMs }: { intervalMs: number }) {
  const router = useRouter();
  useEffect(() => {
    const timer = setInterval(() => {
      router.refresh();
    }, intervalMs);
    return () => clearInterval(timer);
  }, [router, intervalMs]);
  return null;
}
