import { useEffect, useState } from 'react';
import { fetchAgentSettings, probeAgent, saveAgentSettings } from '../lib/api';
import type { AgentSettings } from '../lib/api';

export default function AgentBackendPanel({ onBackendChange }: { onBackendChange: (backend: string) => void }) {
  const [settings, setSettings] = useState<AgentSettings>();
  const [backend, setBackend] = useState('openclaw');
  const [model, setModel] = useState('');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    fetchAgentSettings().then((data) => {
      if (!active) return;
      setSettings(data); setBackend(data.backend); setModel(data.models[data.backend] || '');
      onBackendChange(data.backend);
    }).catch((e) => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [onBackendChange]);
  const selected = settings?.backends.find((row) => row.id === backend);
  const act = async (save: boolean) => {
    setBusy(true); setError(''); setNote('');
    try {
      if (save) {
        const data = await saveAgentSettings(backend, model.trim());
        setSettings(data); onBackendChange(data.backend);
        setNote('已保存。请新建会话使用；已有会话继续使用原后端。');
        window.dispatchEvent(new Event('easel-agent-changed'));
      } else {
        const result = await probeAgent(backend);
        const detail = result.authMode === 'chatgpt' ? `${result.detail}（ChatGPT 订阅登录）` : result.detail;
        if (result.ready) setNote(detail); else setError(detail);
      }
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };
  return <div className="agent-backend-panel">
    <div className="panel-top"><strong>执行助手</strong><span className="desc">选择处理对话与技能的本地助手</span></div>
    <div className="agent-backend-fields">
      <label>助手<select aria-label="执行助手" value={backend} disabled={busy || !settings}
        onChange={(e) => {
          const next = e.target.value;
          setBackend(next); setModel(settings?.models[next] || ''); setNote(''); setError('');
          onBackendChange(next);
        }}>
        {(settings?.backends || []).map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}
      </select></label>
      {backend !== 'openclaw' && <label>模型（可选）<input aria-label="Agent 模型" value={model} disabled={busy}
        placeholder="留空使用 CLI 默认模型" onChange={(e) => setModel(e.target.value)} /></label>}
      <button className="btn btn-sm" disabled={busy || !settings} onClick={() => void act(true)}>保存助手</button>
      <button className="btn btn-sm" disabled={busy || !selected?.installed || backend === 'openclaw'} onClick={() => void act(false)}>
        {busy ? '处理中…' : '检测连接'}
      </button>
    </div>
    {selected && <div className="foot-note">
      {selected.installed ? `已找到：${selected.command.join(' ')}` : `未找到 ${selected.name} CLI。`}
      {backend !== 'openclaw' && <p>使用 CLI 自己的登录与额度；在项目终端运行 <code>.venv/bin/python -m easel agent login {backend}</code> 完成登录。主对话无需填写 API Key。媒体服务仍在对应通道配置。</p>}
      {backend === 'codebuddy' && <p>此入口连接 CodeBuddy Code。WorkBuddy 桌面登录是否共享，以 CLI 实际认证结果为准。</p>}
      {settings?.environmentOverride && <p>当前后端由 EASEL_AGENT_BACKEND 环境变量固定。</p>}
    </div>}
    {note && <p role="status" className="save-note">{note}</p>}
    {error && <p role="alert" className="save-note err">{error}</p>}
  </div>;
}
