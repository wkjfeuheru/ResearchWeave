import { useEffect, useRef, type ReactNode } from 'react';
import { AlertCircle, X } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export function Markdown({ text }: { text: string }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>{text}</ReactMarkdown></div>;
}

export function Modal({ title, children, onClose, wide = false }: {
  title: string; children: ReactNode; onClose: () => void; wide?: boolean;
}) {
  const dialog = useRef<HTMLElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const element = dialog.current;
    (element?.querySelector<HTMLElement>('[data-autofocus]') || element?.querySelector<HTMLElement>('input, textarea, button'))?.focus();
    function keyboard(event: KeyboardEvent) {
      if (event.key === 'Escape') close.current();
      if (event.key !== 'Tab') return;
      const targets = element?.querySelectorAll<HTMLElement>('button:not(:disabled), input, textarea, select, a[href]');
      if (!targets?.length) return;
      const first = targets[0], last = targets[targets.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener('keydown', keyboard);
    return () => { document.removeEventListener('keydown', keyboard); previous?.focus(); };
  }, []);
  return <div className="modal-backdrop" onClick={onClose}>
    <section ref={dialog} className={`modal ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}
      onClick={event => event.stopPropagation()}>
      <header><h2>{title}</h2><button className="icon-button" aria-label="关闭" onClick={onClose}><X size={20} /></button></header>
      {children}
    </section>
  </div>;
}

export function ErrorBanner({ message, onClose }: { message: string; onClose?: () => void }) {
  return <div className="error-banner" role="alert"><AlertCircle size={17} /><span>{message}</span>
    {onClose && <button className="icon-button" aria-label="关闭提示" onClick={onClose}><X size={16} /></button>}
  </div>;
}
