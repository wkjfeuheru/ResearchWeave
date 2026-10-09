import { useEffect, useRef, useState } from 'react';
import type { Message, Prompt, Session } from './api';

export function useConversation(sessionId: string | null, onDone: () => void, onStarted: () => void,
  onFailed: (text: string) => void, onDeleted: () => void) {
  const [session, setSession] = useState<Session | null>(null);
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('');
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  const prompt = prompts[0] || null;
  const [epoch, setEpoch] = useState(0);
  const socket = useRef<WebSocket | null>(null);
  const readySessionId = useRef<string | null>(null);
  const activeId = useRef('');
  const draftId = useRef<string | null>(null);
  const pending = useRef<{ sessionId: string; text: string; profileId: string; attachmentIds: string[] } | null>(null);
  const lastText = useRef('');
  const steering = useRef<{ id: string; text: string } | null>(null);
  const callbacks = useRef({ onDone, onStarted, onFailed, onDeleted });
  callbacks.current = { onDone, onStarted, onFailed, onDeleted };

  function append(row: Message) {
    setSession(current => current ? { ...current, messages: [...current.messages, row] } : current);
  }

  function dispatch(ws: WebSocket, text: string, profileId: string, attachmentIds: string[] = []) {
    activeId.current = crypto.randomUUID();
    lastText.current = text;
    draftId.current = null;
    setBusy(true); setError(''); setStatus('正在准备…');
    ws.send(JSON.stringify({ type: 'submit', request_id: activeId.current, text, profile_id: profileId, attachment_ids: attachmentIds }));
  }

  useEffect(() => {
    readySessionId.current = null;
    setSession(null); setConnected(false); setPrompts([]); setStatus(''); setError('');
    activeId.current = ''; draftId.current = null;
    if (!sessionId) { setBusy(false); return; }
    let disposed = false;
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/sessions/${sessionId}/ws`);
    socket.current = ws;
    ws.onmessage = event => {
      if (disposed) return;
      const data = JSON.parse(event.data);
      if (data.session_id !== sessionId) return;
      if (data.type === 'ready') {
        readySessionId.current = sessionId;
        setSession(data.session); setConnected(true); setBusy(false);
        if (pending.current?.sessionId === sessionId) {
          const queued = pending.current; pending.current = null;
          dispatch(ws, queued.text, queued.profileId, queued.attachmentIds);
        }
        return;
      }
      if (data.type === 'session_deleted') { disposed = true; setSession(null); setBusy(false); setConnected(false); setPrompts([]); callbacks.current.onDeleted(); return; }
      if (data.request_id && data.request_id !== activeId.current && data.request_id !== steering.current?.id) return;
      const pendingSteer = steering.current;
      if (data.type === 'steer_accepted' && pendingSteer && pendingSteer.id === data.next_request_id) {
        // Keep consuming the old run through its final snapshot.
        setPrompts([]); setStatus('正在停止当前执行并重新规划…');
        return;
      }
      switch (data.type) {
        case 'started':
          if (pendingSteer && pendingSteer.id === data.request_id) {
            activeId.current = pendingSteer.id; lastText.current = pendingSteer.text;
            steering.current = null; draftId.current = null;
          }
          setSession(current => current ? { ...current, profile_id: data.profile_id, model: data.model } : current);
          {
            const id = activeId.current, text = lastText.current;
            setSession(current => current && !current.messages.some(row => row.id === id)
              ? { ...current, messages: [...current.messages, { id, role: 'user', text, turn_id: id }] } : current);
          }
          callbacks.current.onStarted(); setStatus('正在思考…'); break;
        case 'delta':
          if (!draftId.current) {
            draftId.current = data.id;
            append({ id: data.id, role: 'assistant', text: data.text, turn_id: data.turn_id, turn_status: 'running', phase: 'pending' });
          } else {
            const id = draftId.current;
            setSession(current => current ? { ...current, messages: current.messages.map(row => row.id === id ? { ...row, text: row.text + data.text } : row) } : current);
          }
          setStatus('正在生成…'); break;
        case 'message': {
          const row = data.message as Message;
          if (row.role === 'assistant') draftId.current = null;
          setSession(current => current ? { ...current, messages: current.messages.some(item => item.id === row.id)
            ? current.messages.map(item => item.id === row.id ? row : item) : [...current.messages, row] } : current);
          break;
        }
        case 'usage':
          setSession(current => current ? { ...current, usage: data.usage } : current); break;
        case 'research_progress':
          setSession(current => current && (!current.research_progress || data.progress.revision > current.research_progress.revision)
            ? { ...current, research_progress: data.progress } : current); break;
        case 'system': append({ id: crypto.randomUUID(), role: 'system', text: data.text }); break;
        case 'status': setStatus(data.message); break;
        case 'prompt': setPrompts(current => current.some(item => item.prompt_id === data.prompt_id) ? current : [...current, data]); break;
        case 'error': setError(data.message); break;
        case 'rejected': steering.current = null; setError(data.message); break;
        case 'clear': setSession(current => current ? { ...current, messages: [] } : current); break;
        case 'done':
          if (pendingSteer && pendingSteer.id === data.request_id) {
            activeId.current = pendingSteer.id; lastText.current = pendingSteer.text;
            steering.current = null;
          }
          setSession(data.session); setBusy(!!steering.current); setPrompts([]);
          setStatus(data.cancelled ? '已停止生成' : '');
          draftId.current = null;
          if (data.failed) callbacks.current.onFailed(lastText.current);
          callbacks.current.onDone(); break;
      }
    };
    ws.onerror = () => { if (!disposed) setError('无法连接对话服务，请检查后端是否已启动'); };
    ws.onclose = () => {
      if (!disposed) {
        readySessionId.current = null;
        setConnected(false); setBusy(false); setPrompts([]); pending.current = null; steering.current = null;
        setSession(current => current ? { ...current, messages: current.messages.map(row => row.turn_status === 'running'
          ? { ...row, turn_status: 'stopped', phase: row.phase === 'pending' ? 'progress' : row.phase,
            status: row.status === 'running' ? 'interrupted' : row.status } : row) } : current);
        setError('连接已断开。重新打开会话可恢复历史；消息不会自动重发。');
      }
    };
    return () => { disposed = true; ws.close(); socket.current = null; readySessionId.current = null; };
  }, [sessionId, epoch]);

  return {
    session, setSession, busy, connected, error, setError, status: prompt ? (prompt.kind === 'question' ? '等待你的回复' : '等待操作确认') : status, prompt,
    reconnect() { setEpoch(value => value + 1); },
    send(id: string, text: string, profileId: string, attachmentIds: string[] = []) {
      setBusy(true);
      // ensureSession can await a catalog refresh while this socket already receives
      // ready. Its caller then resumes with an older render's sessionId/connected.
      if (readySessionId.current === id && socket.current?.readyState === WebSocket.OPEN) dispatch(socket.current, text, profileId, attachmentIds);
      else pending.current = { sessionId: id, text, profileId, attachmentIds };
    },
    cancel() { socket.current?.send(JSON.stringify({ type: 'cancel', request_id: activeId.current })); },
    steer(text: string) {
      if (!text.trim() || !busy || !connected || steering.current || socket.current?.readyState !== WebSocket.OPEN) return;
      const id = crypto.randomUUID();
      steering.current = { id, text: text.trim() };
      socket.current.send(JSON.stringify({ type: 'steer', request_id: id, target_request_id: activeId.current, text: text.trim() }));
      setStatus('正在提交修改要求…');
    },
    respond(answer: string) {
      if (!prompt) return;
      socket.current?.send(JSON.stringify({ type: 'response', request_id: activeId.current, prompt_id: prompt.prompt_id, answer }));
      setPrompts(current => current.filter(item => item.prompt_id !== prompt.prompt_id)); setStatus('正在继续…');
    },
  };
}
