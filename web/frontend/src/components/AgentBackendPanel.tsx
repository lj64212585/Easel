import { useEffect, useState } from 'react';
import { fetchAgentSettings, probeAgent, saveAgentSettings } from '../lib/api';
import type { AgentSelection, AgentSettings } from '../lib/api';
import AgentModelFields from './AgentModelFields';

function AgentCard({ agent, settings, onSaved, onConfigureOpenClaw }: {
  agent: AgentSettings['backends'][number]; settings: AgentSettings;
  onSaved: (settings: AgentSettings) => void; onConfigureOpenClaw: () => void;
}) {
  const [value, setValue] = useState<AgentSelection>({ backend: agent.id, model: agent.model, reasoningEffort: agent.reasoningEffort });
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  const act = async (action: 'save' | 'default' | 'probe') => {
    setBusy(true); setError(''); setNote('');
    try {
      if (action === 'probe') {
        const result = await probeAgent(agent.id);
        const detail = result.authMode === 'chatgpt' ? `${result.detail}（ChatGPT 订阅登录）` : result.detail;
        if (result.ready) setNote(detail); else setError(detail);
      } else {
        const data = await saveAgentSettings(agent.id, value.model, value.reasoningEffort, action === 'default');
        onSaved(data);
        setNote(action === 'default' ? '已设为新对话默认 Agent。' : `已保存 ${agent.name} 默认配置，其他 Agent 配置不变。`);
        window.dispatchEvent(new Event('easel-agent-changed'));
      }
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };
  return <section className="agent-config-card" aria-label={`${agent.name} 配置`}>
    <div className="panel-top"><strong>{agent.name}</strong>
      {settings.backend === agent.id && <span className="pill ok">新对话默认</span>}
      <span className="desc">{agent.installed ? '已检测到 CLI' : '未检测到 CLI'}</span>
    </div>
    <div className="agent-backend-fields">
      {agent.id !== 'openclaw' && <AgentModelFields value={value} onChange={setValue} disabled={busy} labelPrefix="默认" />}
      {agent.id === 'openclaw' && <button className="btn btn-sm" onClick={onConfigureOpenClaw}>配置网关模型</button>}
    </div>
    <div className="agent-card-actions">
      {agent.id !== 'openclaw' && <button className="btn btn-sm" disabled={busy} onClick={() => void act('save')}>保存默认配置</button>}
      <button className="btn btn-sm" disabled={busy || settings.backend === agent.id || settings.environmentOverride} onClick={() => void act('default')}>设为新对话默认</button>
      <button className="btn btn-sm" disabled={busy || !agent.installed} onClick={() => void act('probe')}>{busy ? '处理中…' : '检测连接'}</button>
    </div>
    {agent.id !== 'openclaw' && <div className="foot-note">
      {agent.loginCommand ? <>登录命令：<code>{agent.loginCommand}</code>
        {agent.id === 'codebuddy' && <span>，启动后按提示或输入 <code>/login</code> 登录。</span>}
      </> : '检测到 CLI 后将显示完整登录命令。'}
    </div>}
    {note && <p role="status" className="save-note">{note}</p>}
    {error && <p role="alert" className="save-note err">{error}</p>}
  </section>;
}

export default function AgentBackendPanel({ onBackendChange }: { onBackendChange: (backend: string) => void }) {
  const [settings, setSettings] = useState<AgentSettings>();
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    fetchAgentSettings().then((data) => {
      if (active) { setSettings(data); onBackendChange(data.backend); }
    }).catch((e) => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [onBackendChange]);
  return <div className="agent-backend-panel">
    <div className="panel-top"><strong>执行助手</strong><span className="desc">分别配置多个 Agent，在对话中选择使用</span></div>
    {settings && [...settings.backends].sort((a, b) => Number(a.id === 'openclaw') - Number(b.id === 'openclaw')).map((agent) => <AgentCard key={agent.id} agent={agent} settings={settings}
      onSaved={(data) => { setSettings(data); onBackendChange(data.backend); }} onConfigureOpenClaw={() => onBackendChange('openclaw')} />)}
    {settings?.environmentOverride && <p className="foot-note">新对话默认 Agent 由 EASEL_AGENT_BACKEND 固定；仍可配置和选择其他 Agent。</p>}
    <p className="foot-note">本地助手沿用各自 CLI 的登录与额度，媒体服务独立配置。WorkBuddy 桌面登录不一定与 CodeBuddy CLI 共享。</p>
    {error && <p role="alert" className="save-note err">{error}</p>}
  </div>;
}
