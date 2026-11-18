"use client";

import { useEffect, useRef, useState } from "react";
import { errorText } from "./api";

type Page<T> = { items: T[]; nextCursor?: string | null };
type Entry<T> = Page<T> & { owner: string; busy: boolean; error: string };

export function usePage<T>(key: string | null, load: (signal: AbortSignal, cursor?: string | null) => Promise<Page<T>>) {
  const [entry, setEntry] = useState<Entry<T>>({ owner: "", items: [], busy: false, error: "" });
  const [revision, setRevision] = useState(0);
  const owner = JSON.stringify([key, revision]);
  const loader = useRef(load); loader.current = load;
  const controller = useRef<AbortController | null>(null);
  const activeLoader = useRef<{ owner: string; load: typeof load } | null>(null);
  const loading = useRef(false);
  useEffect(() => {
    if (!key) return;
    const current = new AbortController(); controller.current = current;
    const currentLoad = loader.current;
    activeLoader.current = { owner, load: currentLoad };
    loading.current = true;
    Promise.resolve().then(async () => {
      if (current.signal.aborted) return;
      setEntry({ owner, items: [], busy: true, error: "" });
      try {
        const result = await currentLoad(current.signal);
        if (!current.signal.aborted) setEntry({ ...result, owner, busy: false, error: "" });
      } catch (error) {
        if (!current.signal.aborted) setEntry({ owner, items: [], busy: false, error: errorText(error) });
      } finally { if (controller.current === current) loading.current = false; }
    });
    return () => { current.abort(); if (controller.current === current) loading.current = false; };
  }, [key, owner]);
  async function more() {
    if (!key || entry.owner !== owner || !entry.nextCursor || loading.current) return;
    const current = controller.current;
    if (!current || current.signal.aborted) return;
    const snapshot = activeLoader.current;
    if (!snapshot || snapshot.owner !== owner) return;
    const currentLoad = snapshot.load;
    loading.current = true;
    setEntry(previous => ({ ...previous, busy: true, error: "" }));
    try {
      const result = await currentLoad(current.signal, entry.nextCursor);
      if (!current.signal.aborted) setEntry(previous => previous.owner === owner
        ? { ...previous, items: [...previous.items, ...result.items], nextCursor: result.nextCursor, busy: false }
        : previous);
    } catch (error) {
      if (!current.signal.aborted) setEntry(previous => previous.owner === owner ? { ...previous, busy: false, error: errorText(error) } : previous);
    } finally { if (controller.current === current) loading.current = false; }
  }
  const visible = !!key && entry.owner === owner;
  return { items: visible ? entry.items : [], nextCursor: visible ? entry.nextCursor : null,
    busy: !!key && (!visible || entry.busy), error: visible ? entry.error : "", more,
    refresh: () => { controller.current?.abort(); setRevision(value => value + 1); } };
}
