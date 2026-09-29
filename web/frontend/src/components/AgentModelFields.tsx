import { useEffect, useState } from 'react';
import { fetchAgentOptions } from '../lib/api';
import type { AgentCatalog, AgentSelection } from '../lib/api';

const EFFORT_LABELS: Record<string, string> = {
  none: '不思考', minimal: '极低', low: '低', medium: '中', high: '高', xhigh: '很高', max: '最高', ultra: '超高',
};
const effortLabel = (id: string, name?: string) => EFFORT_LABELS[id] ? `${EFFORT_LABELS[id]} · ${id}` : name || id;

export default function AgentModelFields({ value, onChange, disabled = false, labelPrefix = '' }: {
  value: AgentSelection;
  onChange: (value: AgentSelection) => void;
  disabled?: boolean;
  labelPrefix?: string;
}) {
  const [catalog, setCatalog] = useState<AgentCatalog>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true); setError(''); setCatalog(undefined);
    fetchAgentOptions(value.backend, value.model, revision > 0).then((data) => {
      if (active) { setCatalog(data); if (!data.available) setError(data.detail); }
    }).catch((e) => { if (active) setError(String(e)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [value.backend, value.model, revision]);
  const models = catalog?.models || [];
  const efforts = catalog?.reasoningOptions || [];
  const permissions = catalog?.permissionOptions || [];
  const permissionMode = value.permissionMode || '';
  const permission = permissions.find((p) => p.id === (permissionMode || catalog?.defaultPermissionMode));
  const defaultPermission = permissions.find((p) => p.id === catalog?.defaultPermissionMode)?.name;
  const badModel = !!value.model && !loading && !!catalog?.available && !models.some((m) => m.id === value.model);
  const badEffort = !!value.reasoningEffort && !loading && !!catalog?.available && !efforts.some((e) => e.id === value.reasoningEffort);
  const badPermission = !!permissionMode && !loading && !!catalog?.available && !permissions.some((p) => p.id === permissionMode);
  const defaultModel = models.find((m) => m.id === catalog?.defaultModel)?.name || catalog?.defaultModel;
  return <>
    <label>{labelPrefix}模型<select aria-label={`${labelPrefix}模型`} value={value.model}
      disabled={disabled || loading || value.backend === 'openclaw' || (!models.length && !value.model)}
      onChange={(e) => onChange({ ...value, model: e.target.value, reasoningEffort: '' })}>
      <option value="">{loading ? '正在读取模型…' : value.backend === 'openclaw' ? '使用网关配置' : `CLI 默认${defaultModel ? ` · ${defaultModel}` : ''}`}</option>
      {value.model && !models.some((m) => m.id === value.model) && <option value={value.model} disabled>{value.model}（{loading ? '读取中' : '待验证'}）</option>}
      {models.map((m) => <option key={m.id} value={m.id} title={m.description}>{m.name}</option>)}
    </select></label>
    <label>{labelPrefix}思考深度<select aria-label={`${labelPrefix}思考深度`} value={value.reasoningEffort}
      disabled={disabled || loading || (!efforts.length && !value.reasoningEffort)}
      onChange={(e) => onChange({ ...value, reasoningEffort: e.target.value })}>
      <option value="">{loading ? '读取中…' : efforts.length ? `默认${catalog?.defaultReasoningEffort ? ` · ${effortLabel(catalog.defaultReasoningEffort)}` : ''}` : '由 CLI 管理'}</option>
      {value.reasoningEffort && !efforts.some((e) => e.id === value.reasoningEffort) && <option value={value.reasoningEffort} disabled>{value.reasoningEffort}（待验证）</option>}
      {efforts.map((e) => <option key={e.id} value={e.id} title={e.description}>{effortLabel(e.id, e.name)}</option>)}
    </select></label>
    <label>{labelPrefix}权限<select aria-label={`${labelPrefix}权限`} value={permissionMode} title={permission?.description}
      disabled={disabled || loading || value.backend === 'openclaw' || (!permissions.length && !permissionMode)}
      onChange={(e) => onChange({ ...value, permissionMode: e.target.value })}>
      <option value="">{loading ? '读取中…' : value.backend === 'openclaw' ? '由网关管理' : `默认${defaultPermission ? ` · ${defaultPermission}` : '（由 CLI 管理）'}`}</option>
      {permissionMode && !permissions.some((p) => p.id === permissionMode) && <option value={permissionMode} disabled>{permissionMode}（待验证）</option>}
      {permissions.map((p) => <option key={p.id} value={p.id} title={p.description}>{p.name}</option>)}
    </select></label>
    {value.backend !== 'openclaw' && <button type="button" className="btn btn-sm agent-refresh" title="重新读取 CLI 模型、思考与权限选项"
      disabled={disabled || loading} onClick={() => setRevision((v) => v + 1)}>刷新选项</button>}
    {permission?.description && <div className="agent-options-note">{permission.description}</div>}
    {(error || badModel || badEffort || badPermission) && <div role="status" className="agent-options-note err">{error || '已保存的模型、思考深度或权限当前不可用，请重新选择。'}</div>}
  </>;
}
