import { useEffect, useRef, useState } from 'react';
import { fetchAgentSettings, fetchAgentSelection } from '../lib/api';
import type { AgentSelection, AgentSettings } from '../lib/api';
import AgentModelFields from './AgentModelFields';

export default function ChatAgentControls({ sessionId, value, disabled, hasMessages, onChange }: {
  sessionId: string; value?: AgentSelection; disabled: boolean; hasMessages: boolean;
  onChange: (value: AgentSelection, switchAgent?: boolean) => void;
}) {
  const [settings, setSettings] = useState<AgentSettings>();
  const [error, setError] = useState('');
  const changeRef = useRef(onChange);
  changeRef.current = onChange;
  const valueRef = useRef(value);
  valueRef.current = value;
  useEffect(() => {
    let active = true;
    const refresh = () => {
      void fetchAgentSettings().then(async (data) => {
        if (!active) return;
        setSettings(data); setError('');
        if (!valueRef.current) {
          const saved = await fetchAgentSelection(sessionId);
          if (active && !valueRef.current) changeRef.current(saved);
        }
      }).catch((e) => { if (active) setError(String(e)); });
    };
    refresh();
    window.addEventListener('easel-agent-changed', refresh);
    return () => { active = false; window.removeEventListener('easel-agent-changed', refresh); };
  }, [sessionId]);
  return <div className="chat-agent-controls">
    <div className="agent-backend-fields">
      <label>Agent<select aria-label="对话 Agent" value={value?.backend || ''} disabled={disabled || !settings || !value}
        onChange={(e) => {
          const backend = e.target.value;
          onChange({ backend, model: settings?.models[backend] || '', reasoningEffort: settings?.reasoningEfforts[backend] || '', permissionMode: settings?.permissionModes?.[backend] || '' }, true);
        }}>
        {!value && <option value="">读取配置…</option>}
        {settings?.backends.map((a) => <option key={a.id} value={a.id}>{a.name}{a.installed ? '' : '（未检测到 CLI）'}</option>)}
      </select></label>
      {value && <AgentModelFields value={value} onChange={(next) => onChange(next)} disabled={disabled} />}
    </div>
    {hasMessages && <div className="agent-switch-hint">模型、思考深度与权限对下一轮生效；切换 Agent 会新建对话。</div>}
    {error && <div role="alert" className="agent-options-note err">{error}</div>}
  </div>;
}
