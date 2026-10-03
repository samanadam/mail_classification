import { useCallback, useEffect, useRef, useState } from "react";

let globalShow: (msg: string) => void = () => {};

/** Owns the single toast of the app (mounted once in the shell). */
export function useToast() {
  const [message, setMessage] = useState<string | null>(null);
  const timer = useRef<number>();
  const show = useCallback((msg: string) => {
    setMessage(msg);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setMessage(null), 4500);
  }, []);
  useEffect(() => {
    globalShow = show;
  }, [show]);
  return { message, show };
}

/** Show a toast from any page. */
export function toast(msg: string) {
  globalShow(msg);
}

export function Toast({ message }: { message: string | null }) {
  return message ? <div className="toast" role="status">{message}</div> : null;
}
