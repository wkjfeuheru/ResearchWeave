export interface ModelProfile {
  id: string;
  label: string;
  api_format: string;
  model: string;
  base_url: string | null;
  context_window_tokens?: number | null;
  auto_compact_threshold_tokens?: number | null;
  configured: boolean;
  active: boolean;
  supported: boolean;
  editable: boolean;
  auth_source: string;
  builtin: boolean;
}

export interface Skill {
  id: string;
  label: string;
  description: string;
  version: string;
  author: string;
  enabled: boolean;
  category: string;
  example: string;
  skills: { name: string; description: string; content: string; metadata: { status: string; scope: string; permissions: string[]; required_tools: string[]; optional_tools: string[]; compatible_models: string[]; content_hash: string; deprecation: Record<string, string> | null } }[];
}

export interface SessionFile {
  id: string; name: string; size: number; status: string; gaps?: string[];
  type?: string; kind?: string; task_id?: string | null;
}

export interface Message {
  id: string;
  answer_id?: string;
  role: 'user' | 'assistant' | 'tool' | 'tool_result' | 'system' | 'activity';
  text: string;
  turn_id?: string;
  turn_status?: 'running' | 'completed' | 'stopped' | 'failed';
  phase?: 'pending' | 'progress' | 'final';
  category?: 'read' | 'search' | 'command' | 'fetch' | 'other';
  label?: string;
  target?: string;
  status?: 'running' | 'completed' | 'failed' | 'interrupted';
  outcome?: 'success' | 'partial' | 'empty' | 'error';
  detail?: string;
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  is_error?: boolean;
}

export interface SessionSummary {
  session_id: string;
  summary: string;
  profile_id: string;
  updated_at: number;
}

export interface Usage {
  input_tokens?: number;
  output_tokens?: number;
  cache_read_input_tokens?: number | null;
  cache_creation_input_tokens?: number | null;
  cache_observed_input_tokens?: number;
}

export interface Session extends SessionSummary {
  model: string;
  created_at: number;
  messages: Message[];
  usage: Usage;
  research_progress?: ResearchProgress | null;
}

export interface ResearchProgress {
  revision: number;
  plan_id: string | null;
  title: string;
  current_task_id: string | null;
  tasks: { id: string; title: string; status: 'pending' | 'in_progress' | 'completed' | 'blocked' | 'cancelled';
    blocker: string; completion_note: string; updated_at: string; started_at: string | null; completed_at: string | null }[];
  completed: number;
  total: number;
  replan_required: boolean;
  conflicts?: { id: string; question: string; core: boolean;
    status: 'open' | 'investigating' | 'awaiting_review' | 'resolved' | 'unresolved' | 'interrupted' }[];
}

export interface Prompt {
  kind: 'permission' | 'edit' | 'question';
  prompt_id: string;
  message?: string;
  tool_name?: string;
  tool_label?: string;
  session_scope?: string;
  path?: string;
  diff?: string;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...options.headers },
  });
  const body = await response.json();
  if (!response.ok) {
    throw new Error(typeof body.detail === 'string' ? body.detail : '请求失败，请稍后重试');
  }
  return body as T;
}

export function errorText(error: unknown) {
  return error instanceof Error ? error.message : '操作失败，请稍后重试';
}
