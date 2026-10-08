import { useState } from 'react';
import { Check, Cpu, KeyRound, LoaderCircle, Pencil, Plus, Radio, Trash2 } from 'lucide-react';
import { api, errorText, type ModelProfile } from './api';
import { ErrorBanner, Modal } from './components';

function ModelForm({ profile, onClose, onSaved }: {
  profile: ModelProfile | null; onClose: () => void; onSaved: () => Promise<void>;
}) {
  const [label, setLabel] = useState(profile?.label || '');
  const [format, setFormat] = useState(profile?.api_format === 'anthropic' ? 'anthropic' : 'openai');
  const [model, setModel] = useState(profile?.model || '');
  const [baseUrl, setBaseUrl] = useState(profile?.base_url || '');
  const [key, setKey] = useState('');
  const [windowTokens, setWindowTokens] = useState(profile?.context_window_tokens?.toString() || '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  return <Modal title={profile ? '编辑模型配置' : '添加模型配置'} onClose={() => { if (!saving) onClose(); }}>
    <form onSubmit={async e => {
      e.preventDefault(); setSaving(true); setError('');
      try {
        await api(profile ? `/models/${profile.id}` : '/models', {
          method: profile ? 'PUT' : 'POST', body: JSON.stringify({ label, api_format: format, model, base_url: baseUrl || null, api_key: key || null, context_window_tokens: windowTokens ? Number(windowTokens) : null }),
        });
        setKey(''); await onSaved(); onClose();
      } catch (error) { setError(errorText(error)); } finally { setSaving(false); }
    }}>
      <div className="modal-body form-grid">
        {error && <ErrorBanner message={error} />}
        <label>配置名称<input required maxLength={100} autoFocus value={label} onChange={e => setLabel(e.target.value)} placeholder="例如：我的 DeepSeek" /></label>
        <label>接口类型<select value={format} onChange={e => setFormat(e.target.value)}><option value="openai">OpenAI-compatible</option><option value="anthropic">Anthropic-compatible</option></select></label>
        <label>接口地址 <span className="optional">可选</span><input type="url" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} placeholder={format === 'openai' ? 'https://api.example.com/v1' : 'https://api.example.com'} /><small>留空使用该接口类型的官方地址。</small></label>
        <label>模型名称<input required maxLength={200} value={model} onChange={e => setModel(e.target.value)} placeholder="填写服务商提供的模型 ID" /></label>
        <label>上下文窗口（Token）<input type="number" min={1} step={1} value={windowTokens} onChange={e => setWindowTokens(e.target.value)} placeholder="例如：32768" /><small>填写服务商提供的窗口大小。已识别的模型可留空，其他模型需填写。</small></label>
        <label>API Key<input type="password" autoComplete="new-password" value={key} onChange={e => setKey(e.target.value)} placeholder={profile?.configured ? '已配置，留空保留原密钥' : '输入 API Key'} /><small>密钥保存在本机后端，不保存在浏览器中。</small></label>
      </div>
      <footer className="modal-footer"><button type="button" className="button secondary" onClick={onClose} disabled={saving}>取消</button><button className="button primary" disabled={saving}>{saving && <LoaderCircle size={16} className="spin" />}保存配置</button></footer>
    </form>
  </Modal>;
}

export default function ModelPage({ models, refresh }: { models: ModelProfile[]; refresh: () => Promise<void> }) {
  const [editing, setEditing] = useState<ModelProfile | null | undefined>(undefined);
  const [error, setError] = useState('');
  const [working, setWorking] = useState('');
  const [results, setResults] = useState<Record<string, { ok: boolean; message: string }>>({});
  const supported = models.filter(m => m.supported);
  async function action(id: string, name: string, method: string) {
    setWorking(id); setError('');
    try {
      const result = await api<{ ok: boolean; message: string }>(`/models/${id}${name}`, { method });
      if (name === '/test') setResults(previous => ({ ...previous, [id]: result }));
      await refresh();
    } catch (error) { setError(errorText(error)); } finally { setWorking(''); }
  }
  return <div className="page-content">
    <div className="page-intro"><div><span className="eyebrow">MODEL CONNECTIONS</span><h2>连接你的研究引擎</h2><p>管理模型与接口，为不同研究任务选择合适的配置。</p></div><button className="button primary" onClick={() => setEditing(null)}><Plus size={17} />添加模型</button></div>
    {error && <ErrorBanner message={error} onClose={() => setError('')} />}
    <div className="info-strip"><KeyRound size={18} /><div><strong>配置保存在本机</strong><span>支持 OpenAI 与 Anthropic 兼容接口，可填写自定义服务地址和模型名称。</span></div></div>
    <div className="section-caption"><span>模型配置</span><span>{supported.length} 个配置 · {supported.filter(m => m.configured).length} 个已连接</span></div>
    <div className="model-grid">{supported.map(profile => <article className={`model-card ${profile.active ? 'active' : ''}`} key={profile.id}>
      <div className="card-heading"><div className="card-icon"><Cpu size={22} /></div><div><h3>{profile.label}</h3><span className="muted">{profile.editable ? (profile.api_format === 'anthropic' ? 'Anthropic compatible' : 'OpenAI compatible') : '订阅认证 · 通过 CLI 管理'}</span></div>{profile.active && <span className="badge blue">默认</span>}</div>
      <div className="model-details"><span>模型</span><strong>{profile.model}</strong><span>接口</span><span title={profile.base_url || ''}>{profile.base_url || '官方默认地址'}</span></div>
      <div className={`connection-status ${profile.configured ? 'configured' : ''}`}><span className="status-dot" />{profile.configured ? '凭据已配置' : '等待配置凭据'}</div>
      {results[profile.id] && <div className={`test-result ${results[profile.id].ok ? 'success' : 'failure'}`} role="status">{results[profile.id].message}</div>}
      <div className="card-actions"><button className="button secondary small" disabled={!!working || !profile.configured} onClick={() => action(profile.id, '/test', 'POST')}>{working === profile.id ? <LoaderCircle size={14} className="spin" /> : <Radio size={14} />}测试连接</button>
        {profile.editable && <button className="icon-button" aria-label={`编辑 ${profile.label}`} onClick={() => setEditing(profile)}><Pencil size={16} /></button>}
        {!profile.active && <button className="text-button" disabled={!!working} onClick={() => action(profile.id, '/activate', 'POST')}>设为默认</button>}
        {!profile.builtin && <button className="icon-button danger" aria-label={`删除 ${profile.label}`} disabled={!!working} onClick={() => { if (confirm(`删除模型配置“${profile.label}”？`)) void action(profile.id, '', 'DELETE'); }}><Trash2 size={16} /></button>}
      </div>
      {profile.configured && profile.editable && <button className="clear-key" disabled={!!working} onClick={() => { if (confirm(`清除“${profile.label}”的已保存 API Key？环境变量提供的凭据不受影响。`)) void action(profile.id, '/credential', 'DELETE'); }}>清除已保存密钥</button>}
    </article>)}</div>
    {models.some(m => !m.supported) && <details className="legacy-configs"><summary>其他认证配置 <span className="muted">沿用 CLI 配置，本期网页暂不支持编辑</span></summary><div>{models.filter(m => !m.supported).map(m => <p key={m.id}><Cpu size={15} />{m.label}<span className="tag">{m.active ? 'CLI 当前默认' : '只读'}</span></p>)}</div></details>}
    <p className="page-footnote"><Check size={14} />保存配置后，从下一轮对话开始生效。测试连接会发送一次简短模型请求。</p>
    {editing !== undefined && <ModelForm profile={editing} onClose={() => setEditing(undefined)} onSaved={refresh} />}
  </div>;
}
