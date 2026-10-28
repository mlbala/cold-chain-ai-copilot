import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/client";

export interface PollState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  updatedAt: Date | null;
  refresh: () => void;
}

/** Fetch now, then every `intervalMs`; also refetches whenever `refreshKey` changes. */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  refreshKey: unknown = 0,
): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const mounted = useRef(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await fetcherRef.current();
      if (!mounted.current) return;
      setData(result);
      setError(null);
      setUpdatedAt(new Date());
    } catch (err) {
      if (!mounted.current) return;
      setError(err instanceof ApiError ? err.message : "Request failed");
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    const timer = window.setInterval(() => void load(), intervalMs);
    return () => {
      mounted.current = false;
      window.clearInterval(timer);
    };
  }, [load, intervalMs, refreshKey]);

  return { data, error, loading, updatedAt, refresh: () => void load() };
}
