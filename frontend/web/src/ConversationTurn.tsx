import { useState } from 'react';
import { BookOpen, Check, ChevronRight, Copy, LoaderCircle } from 'lucide-react';
import type { Message } from './api';
import { Markdown } from './components';

const states = { running: '进行中', completed: '已完成', stopped: '已停止', failed: '失败', interrupted: '已中断' };

function ToolGroup({ rows }: { rows: Message[] }) {
  const [open, setOpen] = useState(false);
  const counts = new Map<string, number>();
  rows.forEach(row => counts.set(row.category || 'other', (counts.get(row.category || 'other') || 0) + 1));
  const running = rows.some(row => row.status === 'running');
  const failed = rows.filter(row => row.status === 'failed').length;
  const interrupted = rows.filter(row => row.status === 'interrupted').length;
  const labels: Record<string, (count: number) => string> = {
    read: n => `读取 ${n} 个文件`, search: n => `搜索 ${n} 次`, command: n => `执行 ${n} 条命令`,
    fetch: n => `读取 ${n} 个网页`, other: n => `执行 ${n} 项操作`,
  };
  const summary = [...counts].map(([kind, n]) => labels[kind](n)).join('，');
  return <div className="activity-group">
    <button type="button" className="process-toggle activity-summary" aria-expanded={open} onClick={() => setOpen(!open)}>
      {running && <LoaderCircle size={13} className="spin" />}<span>{running ? '正在' : failed || interrupted ? '' : '已'}{summary}{failed > 0 && ` · ${failed} 项失败`}{interrupted > 0 && ` · ${interrupted} 项已中断`}</span>
      <ChevronRight size={14} className={open ? 'expanded' : ''} />
    </button>
    {open && <ul className="activity-details">{rows.map(row => <li key={row.id}>
      <span className="activity-label">{row.label}</span>{row.target && <code>{row.target}</code>}
      <span className={`activity-status ${row.status}`}>{row.outcome === 'empty' ? '未找到结果' : row.outcome === 'partial' ? '结果不完整' : states[row.status || 'completed']}</span>
      {row.detail && <span className="activity-detail">{row.detail}</span>}
    </li>)}</ul>}
  </div>;
}

export default function ConversationTurn({ rows }: { rows: Message[] }) {
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  const [copied, setCopied] = useState(false);
  const status = rows.at(-1)?.turn_status || 'completed';
  const open = manualOpen ?? status === 'running';
  const process = rows.filter(row => row.role === 'activity' || row.phase === 'progress');
  const answers = rows.filter(row => row.role === 'assistant' && row.phase !== 'progress');
  const segments: Message[][] = [];
  for (const row of process) {
    if (row.role === 'activity' && segments.at(-1)?.[0].role === 'activity') segments.at(-1)!.push(row);
    else segments.push([row]);
  }
  return <article className="message assistant">
    <div className="message-avatar"><BookOpen size={18} /></div>
    <div className="message-body">
      <div className="message-label">ResearchX<span>投研助手</span></div>
      {process.length > 0 && <section className="execution-process" aria-label="执行过程">
        <button type="button" className="process-toggle process-heading" aria-expanded={open} onClick={() => setManualOpen(!open)}>
          {status === 'running' && <LoaderCircle size={14} className="spin" />}<span>执行过程 · {states[status]}</span><ChevronRight size={14} className={open ? 'expanded' : ''} />
        </button>
        {open && <div className="process-content">{segments.map(group => group[0].role === 'activity'
          ? <ToolGroup key={group[0].id} rows={group} /> : <Markdown key={group[0].id} text={group[0].text} />)}</div>}
      </section>}
      {answers.map(row => <div className="answer-content" key={row.id}><Markdown text={row.text} /></div>)}
      {answers.length > 0 && status !== 'running' && <button className="copy-button" aria-label="复制回复" onClick={async () => {
        try { await navigator.clipboard.writeText(answers.map(row => row.text).join('\n\n')); setCopied(true); }
        catch { setCopied(false); }
      }}>{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? '已复制' : '复制'}</button>}
    </div>
  </article>;
}
