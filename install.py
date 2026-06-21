#!/usr/bin/env python3
"""Installer for the model-router plugin.

Goals:
- work for the default Hermes home and all named profiles
- install the plugin in the canonical global location
- symlink named profiles to the canonical plugin dir
- enable the plugin and triage config in each config.yaml
- create skill_routing.md when missing
- append a routing block to SOUL.md when missing
- validate that the local Hermes checkout contains the required CLI patches
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml


PLUGIN_NAME = "model-router"
SOUL_BLOCK_START = "<!-- model-router:start -->"
SOUL_BLOCK_END = "<!-- model-router:end -->"
DEFAULT_ROUTER_CONFIG = {
    "classifier": {
        "provider": "openrouter",
        "model": "qwen/qwen3.5-flash-02-23",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "timeout": 30,
        "extra_body": {"enable_caching": True},
    },
    "tiers": {
        1: {
            "label": "T1 Flash",
            "emoji": "⚡",
            "model": "qwen/qwen3.5-flash-02-23",
            "reasoning": None,
            "role": "fast triage and cheap helper",
            "best_for": [
                "Short acknowledgements",
                "Intent classification",
                "Status checks",
                "Title generation",
            ],
        },
        2: {
            "label": "T2 DeepSeek",
            "emoji": "🔹",
            "model": "deepseek/deepseek-v4-flash",
            "reasoning": None,
            "role": "default daily-driver, navigator",
            "best_for": [
                "Default day-to-day work",
                "Documentation and drafting",
                "Standard coding and research",
            ],
        },
        3: {
            "label": "T3 MiniMax",
            "emoji": "🔷",
            "model": "minimax/minimax-m2.7",
            "reasoning": None,
            "role": "basic coding, creating",
            "best_for": [
                "Debugging",
                "Code review",
                "Large-document synthesis",
                "Complex analysis",
            ],
        },
        4: {
            "label": "T4 DeepSeek Pro",
            "emoji": "🔸",
            "model": "deepseek/deepseek-v4-pro",
            "reasoning": "high",
            "role": "strong reasoning and synthesis",
            "best_for": [
                "Architecture",
                "Migration planning",
                "Complex multi-step design",
                "Nuanced code review",
            ],
        },
        5: {
            "label": "T5 Sonnet",
            "emoji": "🔶🔶",
            "model": "anthropic/claude-sonnet-4-6",
            "reasoning": "medium",
            "role": "expensive deep-think mode",
            "best_for": [
                "Security-sensitive analysis",
                "Algorithmic optimization",
                "High-stakes reasoning",
            ],
        },
    },
    "integrations": {
        "hermes_webui_dir": "",
    },
}


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def info(msg: str) -> None:
    print(f"  [..] {msg}")


def warn(msg: str) -> None:
    print(f"  [warn] {msg}")


def fail(msg: str) -> None:
    print(f"  [fail] {msg}")
    raise SystemExit(1)


def root_home() -> Path:
    """Return the Hermes home root (never a profile subdirectory).

    When HERMES_HOME points at a named profile (e.g. ~/.hermes/profiles/seo),
    we walk up to the real root so canonical plugin dirs and launcher paths
    are resolved correctly.  The named profile is handled by discover_targets().
    """
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        home = Path(env_home).expanduser()
        if home.parent.name == "profiles":
            return home.parent.parent
        return home
    return Path.home() / ".hermes"


def active_profile_from_env() -> str | None:
    """If HERMES_HOME points at a named profile, return its name; else None."""
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        home = Path(env_home).expanduser()
        if home.parent.name == "profiles":
            return home.name
    return None


def canonical_plugin_dir(home_root: Path) -> Path:
    return home_root / "plugins" / PLUGIN_NAME


def plugin_source_dir() -> Path:
    return Path(__file__).resolve().parent


def is_model_router_enabled(home_dir: Path) -> bool:
    cfg = home_dir / "config.yaml"
    if not cfg.exists():
        return False
    text = cfg.read_text(encoding="utf-8")
    return bool(re.search(r"^\s*-\s*model-router\s*$", text, flags=re.MULTILINE))


CLI_SETUP_MIXIN_REF_BLOCK = """            # Ensure plugin manager has a CLI reference in every agent-start path.
            # Interactive run() already sets this, but single-query / -q and quiet
            # mode initialize agents through _init_agent() without entering run().
            # model-router uses this reference to steer the live CLI agent.
            try:
                from hermes_cli.plugins import get_plugin_manager
                get_plugin_manager()._cli_ref = self
            except Exception:
                pass

"""

CLI_INLINE_ROUTING_BLOCK = """                # Handle /model directly on the UI thread so interactive pickers
                # can safely use prompt_toolkit terminal handoff helpers.
                if self._should_handle_model_command_inline(text, has_images=has_images):
                    if not self.process_command(text):
                        self._should_exit = True
                        if event.app.is_running:
                            event.app.exit()
                    event.app.current_buffer.reset(append_to_history=True)
                    # Force a repaint: process_command() prints through
                    # patch_stdout (scrolls output above the prompt) and never
                    # invalidates the app, so the just-cleared input area can
                    # keep showing the submitted text until some unrelated
                    # redraw fires. Every other early-return branch in this
                    # handler invalidates after reset — match them.
                    event.app.invalidate()
                    return

"""

WEBUI_REQUIRED_FILES = (
    "api/routes.py",
    "api/streaming.py",
    "static/index.html",
    "static/commands.js",
    "static/messages.js",
    "static/ui.js",
    "static/style.css",
)
WEBUI_ENV_VARS = (
    "HERMES_WEBUI_DIR",
    "HERMES_WEBUI_ROOT",
    "HERMES_WEBUI_HOME",
)
WEBUI_COMMANDS_ROUTER_HELPERS_BLOCK = """function _getModelRouterAgentCommands(agentCommands){
  const commands=Array.isArray(agentCommands)?agentCommands:[];
  const wanted=new Set(['auto','t1','t2','t3','t4','t5']);
  return commands
    .filter(cmd=>{
      const name=String(cmd&&cmd.name||'').toLowerCase();
      return wanted.has(name)&&!cmd.cli_only&&!cmd.gateway_only;
    })
    .map(cmd=>({
      name:String(cmd.name||'').toLowerCase(),
      desc:String(cmd.description||'').trim()||'model-router session command',
      source:'agent',
      noEcho:false,
    }))
    .sort((a,b)=>{
      const order={auto:0,t1:1,t2:2,t3:3,t4:4,t5:5};
      return (order[a.name]??99)-(order[b.name]??99);
    });
}

"""
WEBUI_COMMANDS_ROUTER_MATCH_BLOCK_OLD = """  if(parsed.kind==='commands') return getMatchingCommands(parsed.query);
"""
WEBUI_COMMANDS_ROUTER_MATCH_BLOCK_NEW = """  if(parsed.kind==='commands'){
    const matches=getMatchingCommands(parsed.query);
    const seen=new Set(matches.map(c=>c.name));
    const agentCommands=(typeof loadAgentCommandMetadata==='function')
      ? await loadAgentCommandMetadata()
      : [];
    for(const cmd of _getModelRouterAgentCommands(agentCommands)){
      if(!cmd.name.startsWith(parsed.query)||seen.has(cmd.name)) continue;
      matches.push(cmd);
      seen.add(cmd.name);
    }
    return matches;
  }
"""
WEBUI_COMMANDS_ROUTER_COMMANDS_OLD = """  {name:'model',     desc:t('cmd_model'),  fn:cmdModel,     arg:'model_name', subArgs:'models', noEcho:true},
"""
WEBUI_COMMANDS_ROUTER_COMMANDS_NEW = """  {name:'model',     desc:t('cmd_model'),  fn:cmdModel,     arg:'model_name', subArgs:'models', noEcho:true},
  {name:'auto',      desc:'Resume model-router auto routing for this session', fn:cmdRouterAuto, noEcho:true},
  {name:'t1',        desc:'Pin this conversation to the configured T1 router slot', fn:()=>cmdRouterTier(1), noEcho:true},
  {name:'t2',        desc:'Pin this conversation to the configured T2 router slot', fn:()=>cmdRouterTier(2), noEcho:true},
  {name:'t3',        desc:'Pin this conversation to the configured T3 router slot', fn:()=>cmdRouterTier(3), noEcho:true},
  {name:'t4',        desc:'Pin this conversation to the configured T4 router slot', fn:()=>cmdRouterTier(4), noEcho:true},
  {name:'t5',        desc:'Pin this conversation to the configured T5 router slot', fn:()=>cmdRouterTier(5), noEcho:true},
"""
WEBUI_COMMANDS_ROUTER_HELPERS_BLOCK_V2 = """async function _runModelRouterCommand(action, tierNum){
  const hasWindow=typeof window!=='undefined';
  const applyTier=hasWindow?window._modelRouterApplyTier:null;
  const resumeAuto=hasWindow?window._modelRouterResumeAuto:null;
  if(action==='auto'&&typeof resumeAuto!=='function'){
    if(typeof showToast==='function') showToast('Model Router integration is not available in this WebUI.', 3000);
    return;
  }
  if(action==='pin'&&typeof applyTier!=='function'){
    if(typeof showToast==='function') showToast('Model Router integration is not available in this WebUI.', 3000);
    return;
  }
  try{
    const result=action==='auto'
      ? await resumeAuto()
      : await applyTier(Number(tierNum)||0);
    const sessionState=result&&result.router&&result.router.session?result.router.session:null;
    const activeTier=sessionState&&sessionState.active_tier?sessionState.active_tier:null;
    let message='Model Router updated';
    if(action==='auto'){
      message='Model Router: auto-routing resumed';
    }else if(activeTier&&activeTier.model){
      message=`Model Router: pinned to ${activeTier.label||('T'+tierNum)} (${activeTier.model})`;
    }else{
      message=`Model Router: pinned to T${tierNum}`;
    }
    if(typeof showToast==='function') showToast(message, 2600);
  }catch(e){
    const detail=e&&e.message?e.message:String(e||'Unknown error');
    if(typeof showToast==='function') showToast(`Model Router failed: ${detail}`, 3600);
  }
}

async function cmdRouterAuto(){
  return _runModelRouterCommand('auto', 0);
}

async function cmdRouterTier(tierNum){
  return _runModelRouterCommand('pin', Number(tierNum)||0);
}

"""
WEBUI_UI_ROUTER_HELPERS_BLOCK_LEGACY = """function _getModelRouterCommandSpecs(){
  const commands=Array.isArray(_agentCommandCache)?_agentCommandCache:[];
  const order=['auto','t1','t2','t3','t4','t5'];
  const labels={auto:'Auto',t1:'T1',t2:'T2',t3:'T3',t4:'T4',t5:'T5'};
  const specs=[];
  for(const name of order){
    const cmd=commands.find(entry=>String(entry&&entry.name||'').toLowerCase()===name);
    if(!cmd||cmd.cli_only||cmd.gateway_only) continue;
    specs.push({
      name,
      label:labels[name]||name.toUpperCase(),
      desc:String(cmd.description||'').trim()||`/${name}`,
    });
  }
  return specs;
}

function _scheduleModelRouterMetadataRefresh(){
  if(typeof loadAgentCommandMetadata!=='function') return;
  if(window._modelRouterMetadataPending) return;
  window._modelRouterMetadataPending=true;
  loadAgentCommandMetadata()
    .then(()=>{ if(typeof _refreshOpenModelDropdown==='function') _refreshOpenModelDropdown(); })
    .catch(()=>{})
    .finally(()=>{ window._modelRouterMetadataPending=false; });
}

async function _sendModelRouterCommand(name){
  const command=String(name||'').trim().toLowerCase();
  if(!command||typeof send!=='function') return;
  const input=$('msg');
  if(!input) return;
  closeModelDropdown();
  input.value=`/${command}`;
  if(typeof autoResize==='function') autoResize();
  try{
    await send();
  }catch(e){
    if(typeof showToast==='function') showToast(`Failed to run /${command}: ${e.message||e}`, 3000);
  }
}

function _appendModelRouterActions(container){
  if(!container) return;
  const specs=_getModelRouterCommandSpecs();
  if(!specs.length){
    _scheduleModelRouterMetadataRefresh();
    return;
  }
  const heading=document.createElement('div');
  heading.className='model-group model-router-group';
  heading.textContent='Model Router';

  const note=document.createElement('div');
  note.className='model-router-note';
  note.textContent='Uses the existing /t1-/t5 and /auto session commands.';

  const row=document.createElement('div');
  row.className='model-router-actions';
  for(const spec of specs){
    const btn=document.createElement('button');
    btn.type='button';
    btn.className='model-router-action';
    btn.textContent=spec.label;
    btn.title=spec.desc;
    btn.onclick=(event)=>{
      event.preventDefault();
      event.stopPropagation();
      void _sendModelRouterCommand(spec.name);
    };
    row.appendChild(btn);
  }

  container.appendChild(heading);
  container.appendChild(note);
  container.appendChild(row);
}

"""
WEBUI_UI_ROUTER_HELPERS_BLOCK = """function _modelRouterUiSessionId(){
  return (S&&S.session&&S.session.session_id)?String(S.session.session_id):'';
}

function _getModelRouterUiState(){
  return (typeof window!=='undefined'&&window._modelRouterUiState&&typeof window._modelRouterUiState==='object')
    ? window._modelRouterUiState
    : null;
}

function _getModelRouterUiSessionState(){
  const state=_getModelRouterUiState();
  const sid=_modelRouterUiSessionId();
  if(!state||!state.session) return null;
  if(sid&&state.session.session_id&&state.session.session_id!==sid) return null;
  return state.session;
}

function _normalizeModelRouterModelId(value){
  return String(value||'').trim().toLowerCase();
}

function _findModelRouterTierByModel(modelId){
  const state=_getModelRouterUiState();
  const tiers=state&&Array.isArray(state.tiers)?state.tiers:[];
  const needle=_normalizeModelRouterModelId(modelId);
  if(!needle) return null;
  for(const tier of tiers){
    if(_normalizeModelRouterModelId(tier&&tier.model)===needle) return tier;
  }
  return null;
}

function _modelRouterCurrentTurnModel(modelId,routing){
  const routed=String(routing&&routing.used_model||'').trim();
  if(routed) return routed;
  const turnModel=String(S&&S.session&&S.session.model_router_turn_model||'').trim();
  if(turnModel) return turnModel;
  const current=String(S&&S.session&&S.session.model||'').trim();
  if(current) return current;
  return String(modelId||'').trim();
}

function _formatModelRouterChipLabel(modelId,labelText,routing){
  const state=_getModelRouterUiState();
  const sessionState=_getModelRouterUiSessionState();
  if(S&&S.session&&(!state||!sessionState||(state.session==null))) _scheduleModelRouterUiRefresh();
  if(!state||!state.enabled) return '';
  if(!sessionState) return '';
  const running=!!(S&&S.busy);
  if(!sessionState.pinned){
    if(!running) return 'Auto';
    const currentModel=_modelRouterCurrentTurnModel(modelId,routing);
    const tier=_findModelRouterTierByModel(currentModel);
    const parts=['Auto'];
    if(tier&&tier.short_label) parts.push(tier.short_label);
    else if(tier&&tier.tier) parts.push(`T${tier.tier}`);
    if(currentModel) parts.push(currentModel);
    return parts.join(' · ');
  }
  const pinnedTier=sessionState.active_tier||_findModelRouterTierByModel(modelId)||_findModelRouterTierByModel(S&&S.session&&S.session.model||'');
  if(!pinnedTier) return '';
  const pinnedShort=String(pinnedTier.short_label||`T${pinnedTier.tier||''}`).trim();
  const pinnedModel=String(pinnedTier.model||modelId||S&&S.session&&S.session.model||'').trim();
  return pinnedModel?`${pinnedShort} · ${pinnedModel}`:pinnedShort;
}

function _modelRouterChipTitle(modelId,routing){
  const state=_getModelRouterUiState();
  const sessionState=_getModelRouterUiSessionState();
  if(S&&S.session&&(!state||!sessionState||(state.session==null))) _scheduleModelRouterUiRefresh();
  if(!state||!state.enabled||!sessionState) return '';
  if(!sessionState.pinned){
    if(!(S&&S.busy)) return 'Model Router auto-routing';
    const currentModel=_modelRouterCurrentTurnModel(modelId,routing);
    const tier=_findModelRouterTierByModel(currentModel);
    const tierText=tier?(tier.label||tier.short_label||`T${tier.tier||''}`):'';
    return currentModel
      ? `Model Router auto-routing${tierText?` (${tierText})`:''}: ${currentModel}`
      : 'Model Router auto-routing';
  }
  const pinnedTier=sessionState.active_tier||_findModelRouterTierByModel(modelId)||_findModelRouterTierByModel(S&&S.session&&S.session.model||'');
  if(!pinnedTier) return 'Model Router pinned';
  return `Model Router pinned: ${pinnedTier.label||pinnedTier.short_label||`T${pinnedTier.tier||''}`} -> ${pinnedTier.model||modelId||''}`;
}

async function _loadModelRouterUiState(force=false){
  const sid=_modelRouterUiSessionId();
  if(!force&&window._modelRouterUiState&&window._modelRouterUiStateSessionKey===sid){
    return window._modelRouterUiState;
  }
  if(!force&&window._modelRouterUiStatePromise&&window._modelRouterUiStatePromiseSessionKey===sid){
    return window._modelRouterUiStatePromise;
  }
  const query=sid?`?session_id=${encodeURIComponent(sid)}`:'';
  window._modelRouterUiStatePromiseSessionKey=sid;
  window._modelRouterUiStatePromise=api(`/api/model-router${query}`)
    .then(data=>{
      const state=(data&&typeof data==='object')?data:{enabled:false,tiers:[],session:null};
      window._modelRouterUiState=state;
      window._modelRouterUiStateSessionKey=sid;
      return state;
    })
    .catch(()=>{
      const state={enabled:false,tiers:[],session:sid?{session_id:sid,pinned:false,auto_routing:true,selected_tier:null,active_tier:null}:null};
      window._modelRouterUiState=state;
      window._modelRouterUiStateSessionKey=sid;
      return state;
    })
    .finally(()=>{
      window._modelRouterUiStatePromise=null;
      window._modelRouterUiStatePromiseSessionKey='';
    });
  return window._modelRouterUiStatePromise;
}

function _scheduleModelRouterUiRefresh(){
  if(window._modelRouterUiRefreshPending) return;
  window._modelRouterUiRefreshPending=true;
  _loadModelRouterUiState(true)
    .then(()=>{
      if(typeof syncModelChip==='function') syncModelChip();
      if(typeof _refreshOpenModelDropdown==='function') _refreshOpenModelDropdown();
    })
    .catch(()=>{})
    .finally(()=>{ window._modelRouterUiRefreshPending=false; });
}

async function _postModelRouterAction(action, tierNum){
  if((!S.session||!S.session.session_id)&&typeof newSession==='function'){
    await newSession();
    if(typeof renderSessionList==='function') await renderSessionList();
  }
  const sid=_modelRouterUiSessionId();
  if(!sid) throw new Error('No active conversation');
  const payload={session_id:sid,action};
  if(action==='pin') payload.tier=Number(tierNum)||0;
  const data=await api('/api/model-router/session',{method:'POST',body:JSON.stringify(payload)});
  if(data&&data.session){
    S.session=data.session;
    if(typeof syncTopbar==='function') syncTopbar();
    if(typeof syncModelChip==='function') syncModelChip();
    if(typeof renderSessionList==='function') await renderSessionList();
  }
  if(data&&data.router){
    window._modelRouterUiState=data.router;
    window._modelRouterUiStateSessionKey=sid;
  }else{
    await _loadModelRouterUiState(true);
  }
  if(typeof _refreshOpenModelDropdown==='function') _refreshOpenModelDropdown();
  return data;
}

async function _applyModelRouterTierFromUi(tierNum){
  closeModelDropdown();
  return _postModelRouterAction('pin', tierNum);
}

async function _resumeModelRouterAutoFromUi(){
  closeModelDropdown();
  return _postModelRouterAction('auto', 0);
}

function _appendModelRouterActions(container){
  if(!container) return;
  const state=_getModelRouterUiState();
  if(!state){
    _scheduleModelRouterUiRefresh();
    return;
  }
  const tiers=Array.isArray(state.tiers)?state.tiers:[];
  if(!state.enabled||!tiers.length) return;
  const sessionState=_getModelRouterUiSessionState();
  const heading=document.createElement('div');
  heading.className='model-group model-router-group';
  heading.textContent='Model Router';

  const note=document.createElement('div');
  note.className='model-router-note';
  note.textContent=sessionState&&sessionState.pinned
    ? 'Pinned tiers stop auto-routing until you choose Auto.'
    : 'Auto uses model-router on the next turn. Tier rows pin the session.';

  const autoRow=document.createElement('div');
  autoRow.className='model-opt model-router-opt'+((sessionState&&!sessionState.pinned)?' active':'');
  autoRow.innerHTML=`<div class="model-opt-top"><span class="model-opt-name">Auto Routing</span><span class="model-opt-badge model-opt-badge--configured">/auto</span></div><span class="model-opt-id">Resume model-router for the next turn</span>`;
  autoRow.onclick=()=>{ void _resumeModelRouterAutoFromUi(); };

  container.appendChild(heading);
  container.appendChild(note);
  container.appendChild(autoRow);

  for(const tier of tiers){
    const active=!!(sessionState&&sessionState.pinned&&Number(sessionState.selected_tier)===Number(tier.tier));
    const row=document.createElement('div');
    row.className='model-opt model-router-opt'+(active?' active':'');
    const badge=active
      ? '<span class="model-opt-badge model-opt-badge--primary">Pinned</span>'
      : `<span class="model-opt-badge model-opt-badge--configured">/${esc(String(tier.short_label||('T'+tier.tier)).toLowerCase())}</span>`;
    const titleParts=[tier.model||''];
    if(tier.reasoning) titleParts.push(`reasoning: ${tier.reasoning}`);
    if(tier.role) titleParts.push(tier.role);
    row.innerHTML=`<div class="model-opt-top"><span class="model-opt-name">${esc(`${tier.emoji?`${tier.emoji} `:''}${tier.label||('T'+tier.tier)}`)}</span>${badge}</div><span class="model-opt-id">${esc(titleParts.filter(Boolean).join(' | '))}</span>`;
    row.onclick=()=>{ void _applyModelRouterTierFromUi(tier.tier); };
    container.appendChild(row);
  }
}

if(typeof window!=='undefined'){
  window._modelRouterRefreshUiState=()=>_loadModelRouterUiState(true);
  window._modelRouterApplyTier=(tierNum)=>_applyModelRouterTierFromUi(tierNum);
  window._modelRouterResumeAuto=()=>_resumeModelRouterAutoFromUi();
}

"""
WEBUI_UI_SYNC_MODEL_CHIP_OLD = """function syncModelChip(){
  const sel=$('modelSelect');
  const chip=$('composerModelChip');
  const label=$('composerModelLabel');
  const mobileLabel=$('composerMobileModelLabel');
  const mobileAction=$('composerMobileModelAction');
  const dd=$('composerModelDropdown');
  if(!sel||!chip||!label) return;
  // Don't show a model label until boot has finished loading to prevent flash of wrong default
  if(!S._bootReady){
    label.textContent='';
    if(mobileLabel) mobileLabel.textContent='';
    chip.title='Conversation model';
    return;
  }
  const opt=_selectedModelOption();
  const text=opt?opt.textContent:getModelLabel(sel.value||'');
  const gatewayRouting=_latestGatewayRoutingForSession(S.session);
  const displayText=_formatGatewayModelLabel(sel.value||'',text,gatewayRouting)||text;
  label.textContent=displayText;
  if(mobileLabel) mobileLabel.textContent=displayText;
  chip.title=gatewayRouting?`${sel.value||'Conversation model'} ${_gatewayRoutingLabel(gatewayRouting)}`:(sel.value||'Conversation model');
  chip.classList.toggle('active',!!(dd&&dd.classList.contains('open')));
  if(mobileAction) mobileAction.classList.toggle('active',!!(dd&&dd.classList.contains('open')));
}
"""
WEBUI_UI_SYNC_MODEL_CHIP_NEW = """function syncModelChip(){
  const sel=$('modelSelect');
  const chip=$('composerModelChip');
  const label=$('composerModelLabel');
  const mobileLabel=$('composerMobileModelLabel');
  const mobileAction=$('composerMobileModelAction');
  const dd=$('composerModelDropdown');
  if(!sel||!chip||!label) return;
  // Don't show a model label until boot has finished loading to prevent flash of wrong default
  if(!S._bootReady){
    label.textContent='';
    if(mobileLabel) mobileLabel.textContent='';
    chip.title='Conversation model';
    return;
  }
  const opt=_selectedModelOption();
  const text=opt?opt.textContent:getModelLabel(sel.value||'');
  const gatewayRouting=_latestGatewayRoutingForSession(S.session);
  const routerLabel=_formatModelRouterChipLabel(sel.value||'',text,gatewayRouting);
  const displayText=routerLabel||_formatGatewayModelLabel(sel.value||'',text,gatewayRouting)||text;
  label.textContent=displayText;
  if(mobileLabel) mobileLabel.textContent=displayText;
  const routerTitle=_modelRouterChipTitle(sel.value||'',gatewayRouting);
  chip.title=routerTitle||(gatewayRouting?`${sel.value||'Conversation model'} ${_gatewayRoutingLabel(gatewayRouting)}`:(sel.value||'Conversation model'));
  chip.classList.toggle('active',!!(dd&&dd.classList.contains('open')));
  if(mobileAction) mobileAction.classList.toggle('active',!!(dd&&dd.classList.contains('open')));
}
"""
WEBUI_UI_ROUTER_INSERT_OLD = """    dd.appendChild(_custRow);
"""
WEBUI_UI_ROUTER_INSERT_NEW = """    dd.appendChild(_custRow);
    _appendModelRouterActions(dd);
"""
WEBUI_STYLE_ROUTER_BLOCK = """
.model-router-note{padding:8px 14px 2px;font-size:11px;color:var(--muted);line-height:1.4;}
.model-router-opt{border-bottom:1px solid var(--border2);}
.model-router-actions{display:flex;flex-wrap:wrap;gap:8px;padding:8px 14px 12px;border-bottom:1px solid var(--border2);}
.model-router-action{appearance:none;border:1px solid var(--border2);background:var(--surface);color:var(--text);border-radius:999px;padding:6px 12px;font:inherit;font-size:12px;font-weight:600;cursor:pointer;transition:background .12s,border-color .12s,color .12s,transform .12s;}
.model-router-action:hover{background:var(--hover-bg);border-color:var(--accent-bg-strong);color:var(--accent-text);transform:translateY(-1px);}
"""
WEBUI_ROUTES_MODEL_ROUTER_HELPERS_BLOCK = """def _model_router_default_config() -> dict:
    return {
        "tiers": {
            1: {
                "label": "T1 Flash",
                "emoji": "⚡",
                "model": "qwen/qwen3.5-flash-02-23",
                "reasoning": None,
                "role": "fast acknowledgements and lightweight tasks",
                "best_for": [],
            },
            2: {
                "label": "T2 DeepSeek",
                "emoji": "🔹",
                "model": "deepseek/deepseek-v4-flash",
                "reasoning": None,
                "role": "default daily-driver",
                "best_for": [],
            },
            3: {
                "label": "T3 MiniMax",
                "emoji": "🔷",
                "model": "minimax/minimax-m2.7",
                "reasoning": None,
                "role": "strong reasoning and synthesis",
                "best_for": [],
            },
            4: {
                "label": "T4 DeepSeek Pro",
                "emoji": "🔸",
                "model": "deepseek/deepseek-v4-pro",
                "reasoning": "high",
                "role": "deliberate fast planner",
                "best_for": [],
            },
            5: {
                "label": "T5 Sonnet",
                "emoji": "🔶",
                "model": "anthropic/claude-sonnet-4.6",
                "reasoning": "medium",
                "role": "expensive deep-think mode",
                "best_for": [],
            },
        }
    }


def _model_router_deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _model_router_deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _model_router_profile_home_for_session(session=None) -> Path:
    try:
        if session is not None and getattr(session, "profile", None):
            from api.profiles import get_hermes_home_for_profile

            return Path(get_hermes_home_for_profile(getattr(session, "profile", None))).expanduser()
    except Exception:
        pass
    try:
        from api.profiles import get_active_hermes_home

        return Path(get_active_hermes_home()).expanduser()
    except Exception:
        return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def _load_model_router_config_for_home(profile_home: Path) -> dict:
    defaults = _model_router_default_config()
    cfg_path = profile_home / "model_router.yaml"
    if not cfg_path.exists():
        return defaults
    try:
        import yaml

        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return defaults
        merged = _model_router_deep_merge(defaults, raw)
        tiers = {}
        raw_tiers = merged.get("tiers", {})
        for tier_num in range(1, 6):
            tier_defaults = copy.deepcopy(defaults["tiers"][tier_num])
            override = raw_tiers.get(tier_num, raw_tiers.get(str(tier_num), {}))
            if not isinstance(override, dict):
                override = {}
            tier_defaults.update(override)
            tiers[tier_num] = tier_defaults
        merged["tiers"] = tiers
        return merged
    except Exception:
        return defaults


def _is_model_router_enabled_for_home(profile_home: Path) -> bool:
    cfg_path = profile_home / "config.yaml"
    if not cfg_path.exists():
        return False
    try:
        import yaml

        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        plugins = raw.get("plugins", {}) if isinstance(raw, dict) else {}
        enabled = plugins.get("enabled", []) if isinstance(plugins, dict) else []
        return "model-router" in enabled
    except Exception:
        return False


def _get_model_router_manager():
    try:
        from hermes_cli.plugins import get_plugin_manager

        manager = get_plugin_manager()
        manager.discover_and_load(force=False)
        if not all(hasattr(manager, name) for name in ("router_pin_session", "router_unpin_session", "router_is_pinned")):
            return None
        return manager
    except Exception:
        return None


def _model_router_tier_entries(config: dict) -> list[dict]:
    tiers = []
    raw_tiers = config.get("tiers", {})
    for tier_num in range(1, 6):
        meta = raw_tiers.get(tier_num, {})
        tiers.append(
            {
                "tier": tier_num,
                "short_label": f"T{tier_num}",
                "label": str(meta.get("label", f"T{tier_num}") or f"T{tier_num}"),
                "emoji": str(meta.get("emoji", "") or ""),
                "model": str(meta.get("model", "") or ""),
                "reasoning": meta.get("reasoning"),
                "role": str(meta.get("role", "") or ""),
                "best_for": [str(item) for item in (meta.get("best_for") or []) if str(item).strip()],
            }
        )
    return tiers


def _model_router_tier_for_model(model: str, tiers: list[dict]) -> int:
    target = str(model or "").strip().lower()
    if not target:
        return 0
    for tier in tiers:
        if str(tier.get("model", "") or "").strip().lower() == target:
            return int(tier.get("tier", 0) or 0)
    return 0


def _model_router_state_payload(session=None) -> dict:
    profile_home = _model_router_profile_home_for_session(session)
    config = _load_model_router_config_for_home(profile_home)
    tiers = _model_router_tier_entries(config)
    manager = _get_model_router_manager()
    enabled = _is_model_router_enabled_for_home(profile_home) and manager is not None

    session_payload = None
    if session is not None:
        sid = str(getattr(session, "session_id", "") or "")
        model = str(getattr(session, "model", "") or "")
        pinned = False
        if enabled:
            try:
                pinned = bool(manager.router_is_pinned(sid))
            except Exception:
                pinned = False
        selected_tier = _model_router_tier_for_model(model, tiers) if pinned else 0
        active_tier = next((tier for tier in tiers if int(tier.get("tier", 0) or 0) == selected_tier), None)
        session_payload = {
            "session_id": sid,
            "model": model,
            "pinned": pinned,
            "auto_routing": not pinned,
            "selected_tier": selected_tier or None,
            "active_tier": active_tier,
        }

    return {
        "enabled": enabled,
        "profile_home": str(profile_home),
        "tiers": tiers,
        "session": session_payload,
    }


def _handle_model_router_session_update(handler, body):
    try:
        require(body, "session_id")
        require(body, "action")
    except ValueError as exc:
        return bad(handler, str(exc))

    sid = str(body.get("session_id", "") or "")
    action = str(body.get("action", "") or "").strip().lower()
    try:
        session = get_session(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)

    profile_home = _model_router_profile_home_for_session(session)
    manager = _get_model_router_manager()
    if not (_is_model_router_enabled_for_home(profile_home) and manager is not None):
        return bad(handler, "model-router is not enabled for this profile", 404)

    if action == "auto":
        try:
            manager.router_unpin_session(sid)
        except Exception as exc:
            return bad(handler, f"Failed to resume auto-routing: {exc}", 500)
        payload = _model_router_state_payload(session)
        return j(handler, {"ok": True, "router": payload, "session": session.compact() | {"messages": session.messages}})

    if action != "pin":
        return bad(handler, "Unsupported model-router action", 400)

    try:
        tier_num = int(body.get("tier", 0) or 0)
    except Exception:
        return bad(handler, "tier must be an integer", 400)
    if tier_num not in (1, 2, 3, 4, 5):
        return bad(handler, "tier must be 1-5", 400)

    config = _load_model_router_config_for_home(profile_home)
    tier_meta = config.get("tiers", {}).get(tier_num, {})
    target_model = str(tier_meta.get("model", "") or "").strip()
    if not target_model:
        return bad(handler, f"T{tier_num} is not configured", 400)

    with _get_session_agent_lock(sid):
        session.model = target_model
        session.save()
    try:
        manager.router_pin_session(sid, target_model)
    except Exception as exc:
        return bad(handler, f"Failed to pin model-router session: {exc}", 500)

    payload = _model_router_state_payload(session)
    return j(handler, {"ok": True, "router": payload, "session": session.compact() | {"messages": session.messages}})


"""
WEBUI_ROUTES_MODEL_ROUTER_GET_OLD = """    if parsed.path == "/api/commands":
"""
WEBUI_ROUTES_MODEL_ROUTER_GET_NEW = """    if parsed.path == "/api/model-router":
        sid = parse_qs(parsed.query).get("session_id", [""])[0]
        session = None
        if sid:
            try:
                session = get_session(sid)
            except KeyError:
                return bad(handler, "Session not found", 404)
        return j(handler, _model_router_state_payload(session))

    if parsed.path == "/api/commands":
"""
WEBUI_ROUTES_MODEL_ROUTER_POST_OLD = """    if parsed.path == "/api/session/update":
"""
WEBUI_ROUTES_MODEL_ROUTER_POST_NEW = """    if parsed.path == "/api/model-router/session":
        return _handle_model_router_session_update(handler, body)

    if parsed.path == "/api/session/update":
"""
WEBUI_ROUTES_MODEL_ROUTER_CHAT_HELPERS_BLOCK = """
def _prepare_model_router_chat_start(session, msg: str, model: str, model_provider):
    profile_home = _model_router_profile_home_for_session(session)
    manager = _get_model_router_manager()
    if not (_is_model_router_enabled_for_home(profile_home) and manager is not None):
        return model, model_provider

    prepare_turn = getattr(manager, "router_prepare_turn", None)
    if not callable(prepare_turn):
        return model, model_provider

    try:
        route = prepare_turn(
            session_id=str(getattr(session, "session_id", "") or ""),
            user_message=str(msg or ""),
            conversation_history=list(getattr(session, "messages", []) or []),
            current_model=str(model or getattr(session, "model", "") or ""),
            platform="webui",
            apply_live=False,
        ) or {}
    except Exception as exc:
        logger.debug("model-router turn preparation failed for webui: %s", exc)
        return model, model_provider

    routed_model = str(route.get("model", "") or "").strip()
    if routed_model:
        return routed_model, model_provider
    return model, model_provider


"""
WEBUI_ROUTES_MODEL_ROUTER_CHAT_PREPARE_OLD = """    if not goal_related and s.session_id in PENDING_GOAL_CONTINUATION:
        goal_related = True
        PENDING_GOAL_CONTINUATION.discard(s.session_id)

    stream_id = uuid.uuid4().hex
"""
WEBUI_ROUTES_MODEL_ROUTER_CHAT_PREPARE_NEW = """    if not goal_related and s.session_id in PENDING_GOAL_CONTINUATION:
        goal_related = True
        PENDING_GOAL_CONTINUATION.discard(s.session_id)

    model, model_provider = _prepare_model_router_chat_start(s, msg, model, model_provider)

    stream_id = uuid.uuid4().hex
"""
WEBUI_ROUTES_MODEL_ROUTER_RESPONSE_OLD = """    if normalized_model:
        response["effective_model"] = model
"""
WEBUI_ROUTES_MODEL_ROUTER_RESPONSE_NEW = """    if normalized_model:
        response["effective_model"] = model
    response["router_turn_model"] = model
"""
WEBUI_STREAMING_ROUTER_BIND_INIT_OLD = """    agent = None
    _live_prompt_estimate_tokens = [0]
"""
WEBUI_STREAMING_ROUTER_BIND_INIT_NEW = """    agent = None
    _router_agent_bound = False
    _live_prompt_estimate_tokens = [0]
"""
WEBUI_STREAMING_ROUTER_BIND_OLD = """            # Prepend workspace context so the agent always knows which directory
"""
WEBUI_STREAMING_ROUTER_BIND_NEW = """            try:
                from hermes_cli.plugins import get_plugin_manager as _get_plugin_manager
                _pm = _get_plugin_manager()
                _bind_router_agent = getattr(_pm, "router_bind_agent", None)
                if callable(_bind_router_agent):
                    _bind_router_agent(session_id, agent)
                    _router_agent_bound = True
            except Exception:
                logger.debug("[webui] model-router live-agent bind failed", exc_info=True)

            # Prepend workspace context so the agent always knows which directory
"""
WEBUI_STREAMING_ROUTER_UNBIND_OLD = """            if _clarify_registered and _unreg_clarify_notify is not None:
                try:
                    _unreg_clarify_notify(session_id)
                except Exception:
                    logger.debug("Failed to unregister clarify callback")
            with _ENV_LOCK:
"""
WEBUI_STREAMING_ROUTER_UNBIND_NEW = """            if _clarify_registered and _unreg_clarify_notify is not None:
                try:
                    _unreg_clarify_notify(session_id)
                except Exception:
                    logger.debug("Failed to unregister clarify callback")
            if _router_agent_bound:
                try:
                    from hermes_cli.plugins import get_plugin_manager as _get_plugin_manager
                    _pm = _get_plugin_manager()
                    _unbind_router_agent = getattr(_pm, "router_unbind_agent", None)
                    if callable(_unbind_router_agent):
                        _unbind_router_agent(session_id, agent)
                except Exception:
                    logger.debug("[webui] model-router live-agent unbind failed", exc_info=True)
            with _ENV_LOCK:
"""
WEBUI_UI_SET_BUSY_OLD = """function setBusy(v){
  S.busy=v;
  updateSendBtn();
  if(!v){
    if(typeof _clearActivityElapsedTimer==='function') _clearActivityElapsedTimer();
    setStatus('');
    setComposerStatus('');
    const sid=_queueDrainSid||(S.session&&S.session.session_id);
    _queueDrainSid=null;
    updateQueueBadge(sid);
    // Drain one queued message for the finished session after UI settles
    const _isViewedSid=!S.session||sid===S.session.session_id;
    const next=sid&&_isViewedSid?shiftQueuedSessionMessage(sid):null;
    if(next){
      updateQueueBadge(sid);
      setTimeout(()=>{
        $('msg').value=next.text||'';
        S.pendingFiles=Array.isArray(next.files)?[...next.files]:[];
        // Restore model from queued item (sent in /api/chat/start payload)
        // Note: profile is NOT restored — full profile switch requires server interaction
        if(next.model&&S.session&&next.model!==S.session.model){
          S.session.model=next.model;
        }
        if(next.model_provider&&S.session) S.session.model_provider=next.model_provider;
        if(next.model&&S.session){
          if(typeof _applyModelToDropdown==='function'&&$('modelSelect')) _applyModelToDropdown(next.model,$('modelSelect'),S.session.model_provider||null);
          if(typeof syncModelChip==='function') syncModelChip();
        }
        autoResize();
        renderTray();
        send();
      },120);
    }
  }
}
"""
WEBUI_UI_SET_BUSY_NEW = """function setBusy(v){
  S.busy=v;
  updateSendBtn();
  if(!v){
    if(S.session) S.session.model_router_turn_model=null;
    if(typeof _clearActivityElapsedTimer==='function') _clearActivityElapsedTimer();
    setStatus('');
    setComposerStatus('');
    const sid=_queueDrainSid||(S.session&&S.session.session_id);
    _queueDrainSid=null;
    updateQueueBadge(sid);
    // Drain one queued message for the finished session after UI settles
    const _isViewedSid=!S.session||sid===S.session.session_id;
    const next=sid&&_isViewedSid?shiftQueuedSessionMessage(sid):null;
    if(next){
      updateQueueBadge(sid);
      setTimeout(()=>{
        $('msg').value=next.text||'';
        S.pendingFiles=Array.isArray(next.files)?[...next.files]:[];
        // Restore model from queued item (sent in /api/chat/start payload)
        // Note: profile is NOT restored — full profile switch requires server interaction
        if(next.model&&S.session&&next.model!==S.session.model){
          S.session.model=next.model;
        }
        if(next.model_provider&&S.session) S.session.model_provider=next.model_provider;
        if(next.model&&S.session){
          if(typeof _applyModelToDropdown==='function'&&$('modelSelect')) _applyModelToDropdown(next.model,$('modelSelect'),S.session.model_provider||null);
          if(typeof syncModelChip==='function') syncModelChip();
        }
        autoResize();
        renderTray();
        send();
      },120);
    }
  }
  if(typeof syncModelChip==='function') syncModelChip();
}
"""
WEBUI_MESSAGES_ROUTER_SYNC_OLD = """    if(S.session&&S.session.session_id===activeSid){
      S.session.active_stream_id = streamId;
    }
"""
WEBUI_MESSAGES_ROUTER_SYNC_NEW = """    if(S.session&&S.session.session_id===activeSid){
      S.session.active_stream_id = streamId;
    }
    if(typeof syncModelChip==='function') syncModelChip();
"""
WEBUI_MESSAGES_EFFECTIVE_MODEL_OLD = """    if(startData.effective_model && S.session){
      S.session.model=startData.effective_model;
      S.session.model_provider=startData.effective_model_provider||S.session.model_provider||null;
      localStorage.setItem('hermes-webui-model', startData.effective_model);
      if(typeof _writePersistedModelState==='function') _writePersistedModelState(startData.effective_model,S.session.model_provider||null);
      if($('modelSelect')) _applyModelToDropdown(startData.effective_model, $('modelSelect'),S.session.model_provider||null);
      if(typeof syncTopbar==='function') syncTopbar();
"""
WEBUI_MESSAGES_EFFECTIVE_MODEL_NEW = """    if(startData.effective_model && S.session){
      S.session.model=startData.effective_model;
      S.session.model_provider=startData.effective_model_provider||S.session.model_provider||null;
      localStorage.setItem('hermes-webui-model', startData.effective_model);
      if(typeof _writePersistedModelState==='function') _writePersistedModelState(startData.effective_model,S.session.model_provider||null);
      if($('modelSelect')) _applyModelToDropdown(startData.effective_model, $('modelSelect'),S.session.model_provider||null);
      if(typeof syncModelChip==='function') syncModelChip();
      if(typeof syncTopbar==='function') syncTopbar();
"""
WEBUI_MESSAGES_ROUTER_TURN_MODEL_OLD = """    streamId=startData.stream_id;
"""
WEBUI_MESSAGES_ROUTER_TURN_MODEL_NEW = """    if(startData.router_turn_model && S.session){
      S.session.model_router_turn_model=startData.router_turn_model;
      if(typeof syncModelChip==='function') syncModelChip();
    }
    streamId=startData.stream_id;
"""
WEBUI_MESSAGES_GATEWAY_ROUTING_OLD = """            if(d.usage.gateway_routing){
              lastAsst._gatewayRouting=d.usage.gateway_routing;
              if(S.session)S.session.gateway_routing=d.usage.gateway_routing;
              if(S.session&&Array.isArray(S.session.gateway_routing_history))S.session.gateway_routing_history.push(d.usage.gateway_routing);
              else if(S.session)S.session.gateway_routing_history=[d.usage.gateway_routing];
            }
"""
WEBUI_MESSAGES_GATEWAY_ROUTING_NEW = """            if(d.usage.gateway_routing){
              lastAsst._gatewayRouting=d.usage.gateway_routing;
              if(S.session)S.session.gateway_routing=d.usage.gateway_routing;
              if(S.session&&Array.isArray(S.session.gateway_routing_history))S.session.gateway_routing_history.push(d.usage.gateway_routing);
              else if(S.session)S.session.gateway_routing_history=[d.usage.gateway_routing];
              if(typeof syncModelChip==='function') syncModelChip();
            }
"""

LAUNCHER_TEMPLATE = """#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME

HERMES_HOME_ROOT="{home_root}"
HERMES_BIN="$HERMES_HOME_ROOT/hermes-agent/venv/bin/hermes"
HERMES_PYTHON="$HERMES_HOME_ROOT/hermes-agent/venv/bin/python"
MODEL_ROUTER_INSTALL="$HERMES_HOME_ROOT/plugins/model-router/install.py"

detect_profile() {{
  local prev=""
  local arg=""
  for arg in "$@"; do
    if [ "$prev" = "-p" ] || [ "$prev" = "--profile" ]; then
      printf '%s' "$arg"
      return 0
    fi
    case "$arg" in
      --profile=*)
        printf '%s' "${{arg#--profile=}}"
        return 0
        ;;
    esac
    prev="$arg"
  done
  printf '%s' "default"
}}

if [ -f "$MODEL_ROUTER_INSTALL" ] && [ -x "$HERMES_PYTHON" ]; then
  PROFILE_NAME="$(detect_profile "$@")"
  STARTUP_STATUS="$("$HERMES_PYTHON" "$MODEL_ROUTER_INSTALL" --startup-status "$PROFILE_NAME" 2>/dev/null || true)"
  if [ "$STARTUP_STATUS" = "invalid" ]; then
    if [ -t 0 ] && [ -t 1 ]; then
      printf '%s' "Hermes model-router startup validation failed. Plugin model-router won't work correctly without repair. Do you want to run install.sh repair now? [y/N] "
      read -r reply
      case "$reply" in
        y|Y|yes|YES)
          if ! "$HERMES_PYTHON" "$MODEL_ROUTER_INSTALL" "$PROFILE_NAME"; then
            printf '%s\\n' "model-router repair failed."
            exit 1
          fi
          STARTUP_STATUS="$("$HERMES_PYTHON" "$MODEL_ROUTER_INSTALL" --startup-status "$PROFILE_NAME" 2>/dev/null || true)"
          if [ "$STARTUP_STATUS" != "ok" ]; then
            printf '%s\\n' "Hermes model-router startup validation still failed after repair."
            exit 1
          fi
          ;;
        *)
          printf '%s\\n' "Continuing without repair. model-router may not work correctly."
          ;;
      esac
    else
      printf '%s\\n' "Hermes model-router startup validation failed and no interactive terminal is available for repair prompt." >&2
    fi
  fi
fi

exec "$HERMES_BIN" "$@"
"""


def _write_with_backup_if_changed(path: Path, new_text: str) -> bool:
    old_text = path.read_text(encoding="utf-8")
    if old_text == new_text:
        return False
    backup = backup_path(path)
    shutil.copy2(path, backup)
    path.write_text(new_text, encoding="utf-8")
    ok(f"Patched {path.name} (backup: {backup.name})")
    return True


def _replace_or_insert_block(
    text: str,
    expected_block: str,
    existing_pattern: str,
    insert_anchor_pattern: str,
    insert_after_match: bool = True,
    missing_error: str = "",
) -> str:
    if expected_block in text:
        return text

    if existing_pattern:
        text = re.sub(existing_pattern, "", text, count=1, flags=re.MULTILINE | re.DOTALL)

    anchor_match = re.search(insert_anchor_pattern, text, flags=re.MULTILINE | re.DOTALL)
    if not anchor_match:
        fail(missing_error or f"Could not locate insertion anchor: {insert_anchor_pattern}")

    insert_at = anchor_match.end() if insert_after_match else anchor_match.start()
    return text[:insert_at] + expected_block + text[insert_at:]


def repair_commands_py(commands_path: Path) -> bool:
    """Remove legacy model-router CommandDef injections from commands.py.

    Current Hermes exposes plugin slash commands through
    PluginContext.register_command(), including CLI autocomplete and gateway
    command surfacing. Keeping /t1-/t5 and /auto as injected built-ins makes
    the plugin command registration conflict and blocks the modern path.
    """
    if not commands_path.exists():
        return False
    text = commands_path.read_text(encoding="utf-8")
    cleaned = re.sub(
        r"^\s*#Model Router\s*\n(?:^\s*$\n|^\s*#Model Router\s*\n)*",
        "",
        text,
        flags=re.MULTILINE,
    )
    cleaned = re.sub(
        r'^\s*CommandDef\("t1".*?^\s*CommandDef\("auto".*?^\s*cli_only=True\),\n?',
        "",
        cleaned,
        flags=re.MULTILINE | re.DOTALL,
    )
    return _write_with_backup_if_changed(commands_path, cleaned)


def repair_cli_setup_mixin_py(mixin_path: Path) -> bool:
    """Patch the current Hermes agent-construction mixin with a CLI reference.

    Hermes moved _init_agent() out of cli.py into
    hermes_cli/cli_agent_setup_mixin.py.  The old installer looked for the
    runtime credential guard in cli.py, which no longer exists.  Patch the
    live agent creation path instead so interactive, single-query, and quiet
    mode all expose get_plugin_manager()._cli_ref for model-router.
    """
    if not mixin_path.exists():
        return False

    text = mixin_path.read_text(encoding="utf-8")
    original = text
    text = _replace_or_insert_block(
        text=text,
        expected_block=CLI_SETUP_MIXIN_REF_BLOCK,
        existing_pattern=(
            r'^\s*# Ensure plugin manager has a CLI reference (?:in every agent-start path|even in non-interactive).*?'
            r'^\s*pass\n+'
        ),
        insert_anchor_pattern=r'^\s*self\.agent\._print_fn = _cprint\n',
        insert_after_match=True,
        missing_error="Could not locate agent print-hook anchor in hermes_cli/cli_agent_setup_mixin.py for model-router patching",
    )

    if text == original:
        return False
    return _write_with_backup_if_changed(mixin_path, text)


def repair_cli_py(cli_path: Path) -> bool:
    if not cli_path.exists():
        return False

    text = cli_path.read_text(encoding="utf-8")
    original = text

    # Current Hermes supports plugin-registered slash commands. Remove the
    # older model-router CLI method/dispatch injection if a previous installer
    # added it, so /t1-/t5 and /auto dispatch through the plugin registry.
    text = re.sub(
        r'^\s*def _handle_tier_pin\(self, tier_cmd: str\) -> None:.*?(?=^\s*def _should_handle_model_command_inline\()',
        "",
        text,
        count=1,
        flags=re.MULTILINE | re.DOTALL,
    )
    text = re.sub(
        r'^\s*elif canonical in \("t1", "t2", "t3", "t4", "t5"\):\n\s*self\._handle_tier_pin\(canonical\)\n\s*elif canonical == "auto":\n\s*self\._handle_auto_routing\(\)\n',
        "",
        text,
        count=1,
        flags=re.MULTILINE,
    )

    text = _replace_or_insert_block(
        text=text,
        expected_block=CLI_INLINE_ROUTING_BLOCK,
        existing_pattern=r'^\s*# Handle /model directly on the UI thread so interactive pickers.*?^\s*return\n\n',
        insert_anchor_pattern=r"^\s*if text or has_images:\n",
        insert_after_match=True,
        missing_error="Could not locate input routing block in cli.py for model-router patching",
    )

    if text == original:
        return False
    return _write_with_backup_if_changed(cli_path, text)


def _is_hermes_webui_root(path: Path) -> bool:
    return path.is_dir() and all((path / rel).exists() for rel in WEBUI_REQUIRED_FILES)


def _configured_webui_roots(targets: list[tuple[str, Path]]) -> list[Path]:
    roots: list[Path] = []
    for _, home_dir in targets:
        cfg_path = router_config_path(home_dir)
        if not cfg_path.exists():
            continue
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        integrations = raw.get("integrations", {})
        if not isinstance(integrations, dict):
            continue
        custom = str(integrations.get("hermes_webui_dir", "") or "").strip()
        if custom:
            roots.append(Path(custom).expanduser())
    return roots


def _candidate_webui_roots(home_root: Path, targets: list[tuple[str, Path]] | None = None) -> list[Path]:
    home = Path.home()
    plugin_root = plugin_source_dir().resolve()
    candidates: list[Path] = []
    for configured in _configured_webui_roots(targets or []):
        candidates.append(configured)
    for env_name in WEBUI_ENV_VARS:
        raw = os.environ.get(env_name, "").strip()
        if raw:
            candidates.append(Path(raw).expanduser())
    if candidates:
        return candidates
    candidates.extend([
        home_root / "hermes-webui",
        home_root / "webui",
        plugin_root.parent.parent / "hermes-webui",
        home / "hermes-webui",
        home / "GitHub" / "hermes-webui",
        home / "code" / "hermes-webui",
        home / "src" / "hermes-webui",
        home / "projects" / "hermes-webui",
    ])
    return candidates


def discover_hermes_webui_roots(home_root: Path, targets: list[tuple[str, Path]] | None = None) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in _candidate_webui_roots(home_root, targets):
        resolved = candidate.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        if _is_hermes_webui_root(resolved):
            roots.append(resolved)
        elif targets:
            for _, home_dir in targets:
                cfg_path = router_config_path(home_dir)
                if not cfg_path.exists():
                    continue
                try:
                    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                except Exception:
                    continue
                integrations = raw.get("integrations", {}) if isinstance(raw, dict) else {}
                configured = str(integrations.get("hermes_webui_dir", "") or "").strip() if isinstance(integrations, dict) else ""
                if configured and Path(configured).expanduser().resolve(strict=False) == resolved:
                    warn(f"Configured hermes-webui path does not look valid: {candidate}")
                    break
    return roots


def _replace_required_snippet(text: str, old: str, new: str, missing_error: str) -> str:
    if new in text:
        return text
    if old not in text:
        fail(missing_error)
    return text.replace(old, new, 1)


def repair_webui_commands_js(commands_path: Path) -> bool:
    if not commands_path.exists():
        return False
    text = commands_path.read_text(encoding="utf-8")
    original = text

    if WEBUI_COMMANDS_ROUTER_HELPERS_BLOCK not in text:
        anchor = "async function getSlashAutocompleteMatches(text){"
        if anchor not in text:
            fail("Could not locate getSlashAutocompleteMatches() in hermes-webui static/commands.js")
        text = text.replace(anchor, WEBUI_COMMANDS_ROUTER_HELPERS_BLOCK + anchor, 1)

    text = _replace_required_snippet(
        text,
        WEBUI_COMMANDS_ROUTER_MATCH_BLOCK_OLD,
        WEBUI_COMMANDS_ROUTER_MATCH_BLOCK_NEW,
        "Could not locate command autocomplete branch in hermes-webui static/commands.js",
    )

    if "name:'auto'" not in text or "name:'t1'" not in text:
        text = _replace_required_snippet(
            text,
            WEBUI_COMMANDS_ROUTER_COMMANDS_OLD,
            WEBUI_COMMANDS_ROUTER_COMMANDS_NEW,
            "Could not locate /model command entry in hermes-webui static/commands.js",
        )

    if WEBUI_COMMANDS_ROUTER_HELPERS_BLOCK_V2 not in text:
        anchor = "async function cmdModel(args){"
        if anchor not in text:
            fail("Could not locate cmdModel() in hermes-webui static/commands.js")
        text = text.replace(anchor, WEBUI_COMMANDS_ROUTER_HELPERS_BLOCK_V2 + anchor, 1)

    if text == original:
        return False
    return _write_with_backup_if_changed(commands_path, text)


def repair_webui_routes_py(routes_path: Path) -> bool:
    if not routes_path.exists():
        return False
    text = routes_path.read_text(encoding="utf-8")
    original = text

    text = text.replace('"model": "qwen/qwen3.6-plus",', '"model": "qwen/qwen3.5-flash-02-23",')

    if WEBUI_ROUTES_MODEL_ROUTER_HELPERS_BLOCK not in text:
        anchor = "def _get_plugin_manager_for_visibility():"
        if anchor not in text:
            fail("Could not locate plugin visibility helpers in hermes-webui api/routes.py")
        text = text.replace(anchor, WEBUI_ROUTES_MODEL_ROUTER_HELPERS_BLOCK + anchor, 1)

    if WEBUI_ROUTES_MODEL_ROUTER_CHAT_HELPERS_BLOCK not in text:
        anchor = "def _handle_model_router_session_update(handler, body):"
        if anchor not in text:
            fail("Could not locate model-router session helper in hermes-webui api/routes.py")
        text = text.replace(anchor, WEBUI_ROUTES_MODEL_ROUTER_CHAT_HELPERS_BLOCK + anchor, 1)

    text = re.sub(
        r"^\s*model, model_provider = _prepare_model_router_chat_start\(s, msg, model, model_provider\)\n",
        "",
        text,
        flags=re.MULTILINE,
    )

    text = _replace_required_snippet(
        text,
        WEBUI_ROUTES_MODEL_ROUTER_GET_OLD,
        WEBUI_ROUTES_MODEL_ROUTER_GET_NEW,
        "Could not locate /api/commands route in hermes-webui api/routes.py",
    )
    text = _replace_required_snippet(
        text,
        WEBUI_ROUTES_MODEL_ROUTER_POST_OLD,
        WEBUI_ROUTES_MODEL_ROUTER_POST_NEW,
        "Could not locate /api/session/update route in hermes-webui api/routes.py",
    )
    text = _replace_required_snippet(
        text,
        WEBUI_ROUTES_MODEL_ROUTER_CHAT_PREPARE_OLD,
        WEBUI_ROUTES_MODEL_ROUTER_CHAT_PREPARE_NEW,
        "Could not locate goal-continuation block in hermes-webui api/routes.py",
    )
    text = _replace_required_snippet(
        text,
        WEBUI_ROUTES_MODEL_ROUTER_RESPONSE_OLD,
        WEBUI_ROUTES_MODEL_ROUTER_RESPONSE_NEW,
        "Could not locate effective_model response block in hermes-webui api/routes.py",
    )

    if text == original:
        return False
    return _write_with_backup_if_changed(routes_path, text)


def repair_webui_streaming_py(streaming_path: Path) -> bool:
    if not streaming_path.exists():
        return False
    text = streaming_path.read_text(encoding="utf-8")
    original = text

    text = _replace_required_snippet(
        text,
        WEBUI_STREAMING_ROUTER_BIND_INIT_OLD,
        WEBUI_STREAMING_ROUTER_BIND_INIT_NEW,
        "Could not locate agent initialization in hermes-webui api/streaming.py",
    )
    text = _replace_required_snippet(
        text,
        WEBUI_STREAMING_ROUTER_BIND_OLD,
        WEBUI_STREAMING_ROUTER_BIND_NEW,
        "Could not locate workspace-context setup in hermes-webui api/streaming.py",
    )
    text = _replace_required_snippet(
        text,
        WEBUI_STREAMING_ROUTER_UNBIND_OLD,
        WEBUI_STREAMING_ROUTER_UNBIND_NEW,
        "Could not locate clarify cleanup block in hermes-webui api/streaming.py",
    )

    if text == original:
        return False
    return _write_with_backup_if_changed(streaming_path, text)


def repair_webui_ui_js(ui_path: Path) -> bool:
    if not ui_path.exists():
        return False
    text = ui_path.read_text(encoding="utf-8")
    original = text

    text = re.sub(
        r"function _modelRouterUiSessionId\(\)\{[\s\S]*?window\._modelRouterResumeAuto=\(\)=>_resumeModelRouterAutoFromUi\(\);\n\}\n+",
        "",
        text,
        flags=re.MULTILINE,
    )
    if WEBUI_UI_ROUTER_HELPERS_BLOCK_LEGACY in text:
        text = text.replace(WEBUI_UI_ROUTER_HELPERS_BLOCK_LEGACY, "", 1)
    anchor = "function renderModelDropdown(){"
    if anchor not in text:
        fail("Could not locate renderModelDropdown() in hermes-webui static/ui.js")
    if WEBUI_UI_ROUTER_HELPERS_BLOCK not in text:
        text = text.replace(anchor, WEBUI_UI_ROUTER_HELPERS_BLOCK + anchor, 1)

    if WEBUI_UI_ROUTER_INSERT_NEW not in text:
        if WEBUI_UI_ROUTER_INSERT_OLD not in text:
            fail("Could not locate model dropdown custom-row insertion in hermes-webui static/ui.js")
        text = text.replace(WEBUI_UI_ROUTER_INSERT_OLD, WEBUI_UI_ROUTER_INSERT_NEW, 1)

    text = _replace_required_snippet(
        text,
        WEBUI_UI_SYNC_MODEL_CHIP_OLD,
        WEBUI_UI_SYNC_MODEL_CHIP_NEW,
        "Could not locate syncModelChip() in hermes-webui static/ui.js",
    )
    if WEBUI_UI_SET_BUSY_NEW not in text:
        if WEBUI_UI_SET_BUSY_OLD in text:
            text = text.replace(WEBUI_UI_SET_BUSY_OLD, WEBUI_UI_SET_BUSY_NEW, 1)
        else:
            set_busy_pattern = (
                r"function setBusy\(v\)\{\n"
                r"  S\.busy=v;\n"
                r"  updateSendBtn\(\);\n"
                r"  if\(!v\)\{\n"
                r"(?P<body>[\s\S]*?)"
                r"  \}\n"
                r"  if\(typeof syncModelChip==='function'\) syncModelChip\(\);\n"
                r"\}\n"
            )
            match = re.search(set_busy_pattern, text, flags=re.MULTILINE)
            if not match:
                fail("Could not locate setBusy() in hermes-webui static/ui.js")
            body = match.group("body")
            if "S.session.model_router_turn_model=null;" not in body:
                body = "    if(S.session) S.session.model_router_turn_model=null;\n" + body
            replacement = (
                "function setBusy(v){\n"
                "  S.busy=v;\n"
                "  updateSendBtn();\n"
                "  if(!v){\n"
                f"{body}"
                "  }\n"
                "  if(typeof syncModelChip==='function') syncModelChip();\n"
                "}\n"
            )
            text = text[:match.start()] + replacement + text[match.end():]

    if text == original:
        return False
    return _write_with_backup_if_changed(ui_path, text)


def repair_webui_messages_js(messages_path: Path) -> bool:
    if not messages_path.exists():
        return False
    text = messages_path.read_text(encoding="utf-8")
    original = text

    text = _replace_required_snippet(
        text,
        WEBUI_MESSAGES_EFFECTIVE_MODEL_OLD,
        WEBUI_MESSAGES_EFFECTIVE_MODEL_NEW,
        "Could not locate effective_model handling in hermes-webui static/messages.js",
    )
    text = _replace_required_snippet(
        text,
        WEBUI_MESSAGES_ROUTER_TURN_MODEL_OLD,
        WEBUI_MESSAGES_ROUTER_TURN_MODEL_NEW,
        "Could not locate stream_id handling in hermes-webui static/messages.js",
    )
    text = _replace_required_snippet(
        text,
        WEBUI_MESSAGES_GATEWAY_ROUTING_OLD,
        WEBUI_MESSAGES_GATEWAY_ROUTING_NEW,
        "Could not locate gateway_routing handling in hermes-webui static/messages.js",
    )
    text = _replace_required_snippet(
        text,
        WEBUI_MESSAGES_ROUTER_SYNC_OLD,
        WEBUI_MESSAGES_ROUTER_SYNC_NEW,
        "Could not locate active_stream_id update in hermes-webui static/messages.js",
    )

    if text == original:
        return False
    return _write_with_backup_if_changed(messages_path, text)


def repair_webui_style_css(style_path: Path) -> bool:
    if not style_path.exists():
        return False
    text = style_path.read_text(encoding="utf-8")
    if WEBUI_STYLE_ROUTER_BLOCK.strip() in text:
        return False
    new_text = text.rstrip() + "\n" + WEBUI_STYLE_ROUTER_BLOCK
    return _write_with_backup_if_changed(style_path, new_text)


def repair_hermes_webui(home_root: Path, targets: list[tuple[str, Path]] | None = None) -> None:
    roots = discover_hermes_webui_roots(home_root, targets)
    if not roots:
        info("No hermes-webui checkout detected; skipping WebUI integration")
        return

    for root in roots:
        info(f"Patching hermes-webui integration: {root}")
        try:
            changed = False
            changed = repair_webui_routes_py(root / "api" / "routes.py") or changed
            changed = repair_webui_streaming_py(root / "api" / "streaming.py") or changed
            changed = repair_webui_commands_js(root / "static" / "commands.js") or changed
            changed = repair_webui_messages_js(root / "static" / "messages.js") or changed
            changed = repair_webui_ui_js(root / "static" / "ui.js") or changed
            changed = repair_webui_style_css(root / "static" / "style.css") or changed
            if not changed:
                ok(f"hermes-webui integration already current: {root}")
        except OSError as exc:
            warn(f"hermes-webui patch skipped for {root}: {exc}")


def repair_hermes_core(home_root: Path) -> None:
    repo = home_root / "hermes-agent"
    cli_path = repo / "cli.py"
    commands_path = repo / "hermes_cli" / "commands.py"
    setup_mixin_path = repo / "hermes_cli" / "cli_agent_setup_mixin.py"
    if not cli_path.exists() or not commands_path.exists() or not setup_mixin_path.exists():
        warn("Hermes source checkout not found; skipping core repair")
        return

    changed = False
    changed = repair_commands_py(commands_path) or changed
    changed = repair_cli_setup_mixin_py(setup_mixin_path) or changed
    changed = repair_cli_py(cli_path) or changed
    if not changed:
        ok("Hermes core integrations already patched")


def collect_missing_core_integrations(home_root: Path) -> list[str]:
    repo = home_root / "hermes-agent"
    cli_path = repo / "cli.py"
    commands_path = repo / "hermes_cli" / "commands.py"
    setup_mixin_path = repo / "hermes_cli" / "cli_agent_setup_mixin.py"
    if not cli_path.exists() or not commands_path.exists() or not setup_mixin_path.exists():
        return []

    cli_text = cli_path.read_text(encoding="utf-8")
    commands_text = commands_path.read_text(encoding="utf-8")
    setup_mixin_text = setup_mixin_path.read_text(encoding="utf-8")

    missing: list[str] = []
    if "CommandDef(\"t1\"" in commands_text or "#Model Router" in commands_text:
        missing.append("legacy model-router slash command block still present in the command registry")
    if CLI_SETUP_MIXIN_REF_BLOCK not in setup_mixin_text:
        missing.append("complete cli_agent_setup_mixin.py _cli_ref block for all agent-start paths")
    if "def _handle_tier_pin" in cli_text or 'canonical in ("t1", "t2", "t3", "t4", "t5")' in cli_text:
        missing.append("legacy model-router /t1-/t5 cli.py dispatch block still present")
    if CLI_INLINE_ROUTING_BLOCK not in cli_text:
        missing.append("complete inline UI-thread routing block for /model and /t1-/t5")
    return missing


def compat_check(home_root: Path) -> None:
    repo = home_root / "hermes-agent"
    cli_path = repo / "cli.py"
    commands_path = repo / "hermes_cli" / "commands.py"
    setup_mixin_path = repo / "hermes_cli" / "cli_agent_setup_mixin.py"
    if not cli_path.exists() or not commands_path.exists() or not setup_mixin_path.exists():
        warn("Hermes source checkout not found; skipping compatibility checks")
        return

    missing = collect_missing_core_integrations(home_root)

    if missing:
        fail(
            "Hermes checkout is missing required model-router integration:\n"
            + "\n".join(f"    - {item}" for item in missing)
        )

    ok("Compatibility check passed")


def sync_global_plugin(home_root: Path) -> Path:
    src = plugin_source_dir()
    dst = canonical_plugin_dir(home_root)

    if src.resolve() == dst.resolve():
        ok(f"Canonical plugin source already in place: {dst}")
        return dst

    dst.parent.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)
    ok(f"Synced plugin source to canonical location: {dst}")
    return dst


def ensure_global_launcher(home_root: Path) -> None:
    default_home = Path.home() / ".hermes"
    if home_root != default_home:
        info("Skipping launcher patch because HERMES_HOME is not the default ~/.hermes")
        return

    launcher_path = Path.home() / ".local" / "bin" / "hermes"
    launcher_path.parent.mkdir(parents=True, exist_ok=True)
    new_text = LAUNCHER_TEMPLATE.format(home_root=str(home_root))
    if launcher_path.exists():
        old_text = launcher_path.read_text(encoding="utf-8")
        if old_text == new_text:
            ok("Global hermes launcher already current")
            return
        backup = backup_path(launcher_path)
        shutil.copy2(launcher_path, backup)
        launcher_path.write_text(new_text, encoding="utf-8")
        launcher_path.chmod(0o755)
        ok(f"Updated global hermes launcher (backup: {backup.name})")
        return

    launcher_path.write_text(new_text, encoding="utf-8")
    launcher_path.chmod(0o755)
    ok("Created global hermes launcher with model-router startup guard")


def discover_targets(home_root: Path, argv: list[str]) -> list[tuple[str, Path]]:
    """Return (name, path) pairs for every target to install into.

    Special case: when HERMES_HOME=~/.hermes/profiles/<name> (i.e. the caller
    is already running *inside* a named profile), active_profile_from_env()
    returns that profile name.  Without this check, discover_targets() would
    only add the profile as part of the auto-scan — but the auto-scan walks
    home_root/profiles/, and home_root is the *root* (~/.hermes), so the
    named profile *is* discovered correctly in the no-argv branch.

    The real bug was subtler: install.sh was invoked with HERMES_HOME set to
    the profile dir, root_home() stripped the profile suffix, and then
    ensure_profile_plugin_link() skipped name=="default" — so the active
    named profile never got its symlink.  We fix this by ensuring that when
    HERMES_HOME points at a named profile and no explicit argv is given, that
    profile appears in the target list under its real name (not "default").
    """
    if argv:
        targets: list[tuple[str, Path]] = []
        for name in argv:
            canon = name.strip()
            if not canon:
                continue
            if canon == "default":
                targets.append(("default", home_root))
            else:
                targets.append((canon, home_root / "profiles" / canon))
        return targets

    # No explicit targets — auto-discover.
    targets: list[tuple[str, Path]] = [("default", home_root)]
    profiles_dir = home_root / "profiles"
    if profiles_dir.is_dir():
        for child in sorted(profiles_dir.iterdir()):
            if child.is_dir() and (child / "config.yaml").exists():
                targets.append((child.name, child))

    # If the caller is running inside a named profile (HERMES_HOME=.../profiles/<name>)
    # and that profile was NOT found in the auto-scan (e.g. its config.yaml lives
    # somewhere unexpected), add it explicitly so its symlink is always created.
    active = active_profile_from_env()
    if active and active != "default":
        already = any(name == active for name, _ in targets)
        if not already:
            profile_path = home_root / "profiles" / active
            if profile_path.is_dir():
                targets.append((active, profile_path))
                info(f"Added active profile '{active}' from HERMES_HOME to install targets")

    return targets


def backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.name}.bak.model-router.{stamp}")


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _classifier_api_key_env(merged: dict, raw: dict | None) -> str:
    classifier = merged.get("classifier") if isinstance(merged, dict) else {}
    if not isinstance(classifier, dict):
        classifier = {}
    raw_classifier = (raw or {}).get("classifier", {}) if isinstance(raw, dict) else {}
    if not isinstance(raw_classifier, dict):
        raw_classifier = {}
    api_key_env = str(
        raw_classifier.get("api_key_env")
        or raw_classifier.get("key_env")
        or classifier.get("api_key_env")
        or classifier.get("key_env")
        or "OPENROUTER_API_KEY"
    ).strip()
    return api_key_env or "OPENROUTER_API_KEY"


def _normalize_classifier_config(merged: dict, raw: dict | None) -> None:
    """Normalize classifier config and remove legacy inline api_key secrets.

    model_router.yaml stores only the env-var *name* in classifier.api_key_env.
    The actual secret belongs in Hermes' environment (~/.hermes/.env or the
    process env) and is synced to config.yaml as ${ENV_NAME} so Hermes resolves
    it at runtime without persisting the value in YAML.
    """
    classifier = merged.get("classifier")
    if not isinstance(classifier, dict):
        classifier = {}
        merged["classifier"] = classifier

    classifier["api_key_env"] = _classifier_api_key_env(merged, raw)
    classifier.pop("key_env", None)
    classifier.pop("api_key", None)


def _env_file_value(env_path: Path, key: str) -> str:
    if not env_path.exists():
        return ""
    try:
        for line in env_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, value = stripped.split("=", 1)
            if name.strip() == key:
                return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _write_env_file_value(env_path: Path, key: str, value: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    updated = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _old = stripped.split("=", 1)
        if name.strip() == key:
            lines[idx] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass


def migrate_classifier_api_key_to_env(home_dir: Path, raw: dict | None, normalized: dict) -> None:
    """Move a legacy classifier.api_key value into the configured env var.

    Does nothing when the legacy key is empty or the target env var is already
    set in the process or in the profile's .env file.
    """
    raw_classifier = (raw or {}).get("classifier", {}) if isinstance(raw, dict) else {}
    if not isinstance(raw_classifier, dict):
        return
    legacy_key = str(raw_classifier.get("api_key") or "").strip()
    if not legacy_key:
        return
    api_key_env = _classifier_api_key_env(normalized, raw)
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", api_key_env):
        fail(f"Invalid classifier.api_key_env in model_router.yaml: {api_key_env!r}")
    env_path = home_dir / ".env"
    if os.environ.get(api_key_env) or _env_file_value(env_path, api_key_env):
        warn(f"{home_dir.name if home_dir.name != '.hermes' else 'default'}: removed classifier.api_key from model_router.yaml; {api_key_env} already exists in environment")
        return
    _write_env_file_value(env_path, api_key_env, legacy_key)
    ok(f"{home_dir.name if home_dir.name != '.hermes' else 'default'}: moved classifier API key to {env_path.name} as {api_key_env}")


def normalize_router_config(raw: dict | None) -> dict:
    merged = _deep_merge(DEFAULT_ROUTER_CONFIG, raw or {})
    _normalize_classifier_config(merged, raw)
    normalized_tiers: dict[int, dict] = {}
    raw_tiers = merged.get("tiers", {})
    for tier_num in range(1, 6):
        tier_defaults = copy.deepcopy(DEFAULT_ROUTER_CONFIG["tiers"][tier_num])
        override = raw_tiers.get(tier_num, raw_tiers.get(str(tier_num), {}))
        if not isinstance(override, dict):
            override = {}
        tier_defaults.update(override)
        normalized_tiers[tier_num] = tier_defaults
    merged["tiers"] = normalized_tiers
    return merged


def router_config_path(home_dir: Path) -> Path:
    return home_dir / "model_router.yaml"


def render_router_config(router_config: dict) -> str:
    return yaml.safe_dump(
        router_config,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )


def ensure_router_config(home_dir: Path) -> dict:
    path = router_config_path(home_dir)
    raw: dict | None = None
    config: dict = {}
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError("model_router.yaml must be a mapping")
            config = normalize_router_config(raw)
        except Exception as exc:
            fail(f"{path} is invalid: {exc}")
    else:
        raw = {}
        config = normalize_router_config(raw)

    migrate_classifier_api_key_to_env(home_dir, raw, config)
    rendered = render_router_config(config)
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    if existing != rendered:
        path.write_text(rendered, encoding="utf-8")
        action = "created" if existing is None else "normalized"
        ok(f"{home_dir.name if home_dir.name != '.hermes' else 'default'}: {action} model_router.yaml")
    else:
        ok(f"{home_dir.name if home_dir.name != '.hermes' else 'default'}: model_router.yaml already current")
    return config


def ensure_profile_plugin_link(name: str, home_dir: Path, canonical_dir: Path) -> None:
    if name == "default":
        return

    plugins_dir = home_dir / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    link = plugins_dir / PLUGIN_NAME

    if link.is_symlink():
        target = link.resolve()
        if target == canonical_dir.resolve():
            ok(f"{name}: plugin symlink already points to canonical source")
            return
        link.unlink()
        link.symlink_to(canonical_dir)
        ok(f"{name}: updated plugin symlink -> {canonical_dir}")
        return

    if link.exists():
        backup = backup_path(link)
        shutil.move(str(link), str(backup))
        warn(f"{name}: existing plugin dir/file moved to {backup}")

    link.symlink_to(canonical_dir)
    ok(f"{name}: created plugin symlink -> {canonical_dir}")


def ensure_plugin_enabled(text: str) -> tuple[str, bool]:
    if re.search(r"^\s*-\s*model-router\s*$", text, flags=re.MULTILINE):
        return text, False

    plugins_match = re.search(r"^plugins:\n", text, flags=re.MULTILINE)
    if not plugins_match:
        return text.rstrip("\n") + "\nplugins:\n  enabled:\n    - model-router\n", True

    enabled_match = re.search(r"^plugins:\n(?:  .*\n)*?  enabled:\n", text, flags=re.MULTILINE)
    if enabled_match:
        insert_at = enabled_match.end()
        return text[:insert_at] + "    - model-router\n" + text[insert_at:], True

    insert_at = plugins_match.end()
    return text[:insert_at] + "  enabled:\n    - model-router\n" + text[insert_at:], True


def render_triage_specifier_block(router_config: dict) -> str:
    classifier = router_config["classifier"]
    api_key_env = str(classifier.get("api_key_env") or "OPENROUTER_API_KEY").strip() or "OPENROUTER_API_KEY"
    lines = [
        "  triage_specifier:",
        f"    provider: {classifier['provider']}",
        f"    model: {classifier['model']}",
        f"    base_url: {classifier['base_url']}",
        f"    api_key: ${{{api_key_env}}}",
        f"    timeout: {classifier['timeout']}",
        "    extra_body:",
    ]
    extra_body = classifier.get("extra_body", {})
    if extra_body:
        for key, value in extra_body.items():
            rendered = "true" if value is True else "false" if value is False else value
            lines.append(f"      {key}: {rendered}")
    else:
        lines.append("      {}")
    return "\n".join(lines) + "\n"


def ensure_triage_specifier(text: str, router_config: dict) -> tuple[str, bool]:
    block = render_triage_specifier_block(router_config)
    pattern = r"^  triage_specifier:\n(?:    .*\n|      .*\n)*"
    if re.search(pattern, text, flags=re.MULTILINE):
        updated = re.sub(pattern, block, text, count=1, flags=re.MULTILINE)
        return updated, updated != text

    aux_match = re.search(r"^auxiliary:\n", text, flags=re.MULTILINE)
    if not aux_match:
        return text.rstrip("\n") + "\nauxiliary:\n" + block, True

    insert_at = aux_match.end()
    return text[:insert_at] + block + text[insert_at:], True


def ensure_config(home_dir: Path, router_config: dict) -> None:
    cfg = home_dir / "config.yaml"
    if not cfg.exists():
        warn(f"{home_dir}: missing config.yaml, skipping config patch")
        return

    text = cfg.read_text(encoding="utf-8")
    changed = False
    text, delta = ensure_plugin_enabled(text)
    changed = changed or delta
    text, delta = ensure_triage_specifier(text, router_config)
    changed = changed or delta
    if changed:
        cfg.write_text(text, encoding="utf-8")
        ok(f"{home_dir.name if home_dir.name != '.hermes' else 'default'}: updated config.yaml")
    else:
        ok(f"{home_dir.name if home_dir.name != '.hermes' else 'default'}: config.yaml already configured")


def render_skill_routing(home_dir: Path, router_config: dict) -> str:
    classifier = router_config["classifier"]
    lines = [
        "# Hermes Model Routing Policy\n\n"
        "This file documents the cost-aware routing tiers used by the model-router plugin.\n"
        "Routing is AUTOMATIC -- the plugin classifies each turn by complexity and switches\n"
        "the model before every LLM call. No manual /model switching needed.\n\n"
        "Plugin location: active Hermes home `plugins/model-router`\n"
        "Router config:    active Hermes home `model_router.yaml`\n"
        f"Classifier:       {classifier['provider']} / {classifier['model']}\n\n"
        "## Goals\n\n"
        "- Default to the cheapest model that can likely complete the task well.\n"
        "- Escalate only when the task actually needs deeper reasoning or better context handling.\n"
        "- Keep expensive Sonnet usage rare and intentional.\n\n"
        "## Tier Table\n\n"
    ]
    for tier_num in range(1, 6):
        meta = router_config["tiers"][tier_num]
        lines.append(f"Tier {tier_num}\n")
        lines.append(f"  Label:     {meta['label']}\n")
        lines.append(f"  Model:     {meta['model']}\n")
        if meta.get("reasoning"):
            lines.append(f"  Reasoning: {meta['reasoning']}\n")
        lines.append(f"  Role:      {meta.get('role', '')}\n")
        lines.append("  Triggers:\n")
        for item in meta.get("best_for", []):
            lines.append(f"    - {item}\n")
        lines.append("\n")

    lines.extend(
        [
            "## Escalation Rules\n\n",
            "1. Start with Tier 2 for most normal user work.\n",
            "2. Use Tier 1 for ultra-short acks, triage, or mechanical helper tasks.\n",
            "3. Escalate to Tier 3 when debugging, reviewing, or handling large docs.\n",
            "4. Escalate to Tier 4 for design, planning, or architecture work.\n",
            "5. Escalate to Tier 5 only for security-critical or algorithmically dense tasks.\n",
            "6. Fail fast: if unsure, pick the cheaper tier first.\n\n",
            "## Cost Notes\n\n",
            "- Prompt caching should remain enabled.\n",
            "- Keep cheap models on auxiliary tasks.\n",
            "- Avoid using Sonnet for rote edits, summaries, or shell/file boilerplate.\n",
        ]
    )
    return "".join(lines)


def ensure_skill_routing(home_dir: Path, router_config: dict) -> None:
    path = home_dir / "skill_routing.md"
    rendered = render_skill_routing(home_dir, router_config)
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    if existing != rendered:
        path.write_text(rendered, encoding="utf-8")
        action = "created" if existing is None else "updated"
        ok(f"{home_dir.name if home_dir.name != '.hermes' else 'default'}: {action} skill_routing.md")
    else:
        ok(f"{home_dir.name if home_dir.name != '.hermes' else 'default'}: skill_routing.md already current")


def render_soul_block(home_dir: Path) -> str:
    skill_path = home_dir / "skill_routing.md"
    return (
        f"{SOUL_BLOCK_START}\n"
        "When multiple model tiers are available, prefer the cheapest tier that is likely to succeed.\n"
        "Default to the day-to-day model first, then escalate only when the task truly needs deeper reasoning, better long-context handling, or higher confidence.\n\n"
        f"Use `{skill_path}` as the shared local routing policy for model escalation.\n\n"
        "Routing posture:\n"
        "- Keep cheap helper tasks on the lightest tier.\n"
        "- Use the default tier for most ordinary work.\n"
        "- Escalate to stronger tiers for debugging, architecture, security, and unusually complex reasoning.\n"
        "- If uncertain between two tiers, start with the cheaper one and escalate only after weak output or clear mismatch.\n"
        "- Apply fail-fast routing: if task complexity is unclear, choose the lowest plausible tier first.\n"
        "- Treat one cheap failed attempt as acceptable; escalate after mismatch, weak output, or repeated failure.\n"
        f"{SOUL_BLOCK_END}\n"
    )


def ensure_soul(home_dir: Path) -> None:
    path = home_dir / "SOUL.md"
    block = render_soul_block(home_dir)
    if not path.exists():
        path.write_text("# Hermes Agent Persona\n\n" + block, encoding="utf-8")
        ok(f"{home_dir.name if home_dir.name != '.hermes' else 'default'}: created SOUL.md")
        return

    text = path.read_text(encoding="utf-8")
    if SOUL_BLOCK_START in text and SOUL_BLOCK_END in text:
        new_text = re.sub(
            re.escape(SOUL_BLOCK_START) + r".*?" + re.escape(SOUL_BLOCK_END) + r"\n?",
            block,
            text,
            flags=re.DOTALL,
        )
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            ok(f"{home_dir.name if home_dir.name != '.hermes' else 'default'}: refreshed router block in SOUL.md")
        else:
            ok(f"{home_dir.name if home_dir.name != '.hermes' else 'default'}: SOUL.md router block already current")
        return

    legacy_pattern = (
        r"When multiple model tiers are available, prefer the cheapest tier that is likely to succeed\.\n"
        r".*?"
        r"- Treat one cheap failed attempt as acceptable; escalate after mismatch, weak output, or repeated failure\.\n?"
    )
    if re.search(legacy_pattern, text, flags=re.DOTALL):
        new_text = re.sub(legacy_pattern, block, text, count=1, flags=re.DOTALL)
        path.write_text(new_text, encoding="utf-8")
        ok(f"{home_dir.name if home_dir.name != '.hermes' else 'default'}: replaced legacy routing block in SOUL.md")
        return

    sep = "" if text.endswith("\n\n") else "\n\n"
    path.write_text(text + sep + block, encoding="utf-8")
    ok(f"{home_dir.name if home_dir.name != '.hermes' else 'default'}: appended router block to SOUL.md")


def startup_status(home_root: Path, profile_name: str) -> str:
    target_home = home_root if profile_name == "default" else home_root / "profiles" / profile_name
    if not is_model_router_enabled(target_home):
        return "disabled"
    if not router_config_path(target_home).exists():
        return "invalid"
    # Also check that the profile symlink exists (can be missing if the profile
    # was created after install.sh ran — the auto-scan skips profiles without
    # config.yaml at install time, so a late-added profile never gets its link).
    if profile_name != "default":
        link = target_home / "plugins" / PLUGIN_NAME
        if not link.exists():
            return "invalid"
    return "invalid" if collect_missing_core_integrations(home_root) else "ok"


def install_for_target(name: str, home_dir: Path, canonical_dir: Path) -> None:
    if not home_dir.exists():
        warn(f"{name}: target home does not exist at {home_dir}, skipping")
        return

    print(f"\n=== Target: {name} ({home_dir}) ===")
    router_config = ensure_router_config(home_dir)
    ensure_profile_plugin_link(name, home_dir, canonical_dir)
    ensure_config(home_dir, router_config)
    ensure_skill_routing(home_dir, router_config)
    ensure_soul(home_dir)


def main(argv: list[str]) -> int:
    home_root = root_home()
    if argv and argv[0] == "--startup-status":
        profile_name = argv[1] if len(argv) > 1 and argv[1].strip() else "default"
        print(startup_status(home_root, profile_name))
        return 0

    targets = discover_targets(home_root, argv)
    repair_hermes_core(home_root)
    compat_check(home_root)
    canonical_dir = sync_global_plugin(home_root)
    ensure_global_launcher(home_root)
    repair_hermes_webui(home_root, targets)

    if not targets:
        warn("No install targets found")
        return 0

    info("Targets: " + ", ".join(name for name, _ in targets))
    for name, home_dir in targets:
        install_for_target(name, home_dir, canonical_dir)

    print("\nDone. Restart Hermes for changes to take effect.")
    print("Verify with: hermes plugins list")
    print("Named profiles use: <profile> plugins list")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
