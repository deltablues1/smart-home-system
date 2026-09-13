/* Small, read-only energy cards for the wall panel. No external dependencies. */
class JarvisEnergyCard extends HTMLElement {
  setConfig(config) {
    this.config = config;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this.signature = null;
    this.render();
  }

  set hass(hass) {
    this._hass = hass;
    const signature = JSON.stringify(Object.values(this.config?.entities || {}).map(id => hass.states[id]?.state));
    if (signature !== this.signature) {
      this.signature = signature;
      this.render();
    }
  }

  number(key) {
    const state = this._hass?.states[this.config.entities[key]]?.state;
    return state !== undefined && state !== "" && Number.isFinite(Number(state)) ? Number(state) : null;
  }

  format(value, digits = 0) {
    return value === null ? "—" : value.toLocaleString("hr-HR", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  value(key, unit, digits = 0) {
    return `${this.format(this.number(key), digits)} <small>${unit}</small>`;
  }

  getCardSize() { return this.config.mode === "detail" ? 7 : 3; }

  render() {
    if (!this.config || !this.shadowRoot) return;
    const detail = this.config.mode === "detail";
    const meter = this.config.mode === "meter";
    const grid = this.config.mode === "grid";
    const totals = this.config.mode === "totals";
    const energy = this.number("energy_house");
    const reference = Number(this.config.meter_reference);
    const escape = text => String(text ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const floor = (key, label, icon) => `<button class="floor" data-key="power_${key}">
      <span class="label"><ha-icon icon="${icon}"></ha-icon>${label}</span>
      <strong>${this.value(`power_${key}`, "W")}</strong>
    </button>`;
    this.shadowRoot.innerHTML = `<style>
      :host { display:block; height:100%; color:var(--primary-text-color); }
      * { box-sizing:border-box; }
      ha-card { height:100%; padding:10px 16px; overflow:visible; }
      ha-card.detail { padding:16px 18px; }
      button { font:inherit; color:inherit; cursor:pointer; border:0; background:none; text-align:left; padding:0; }
      button:focus-visible { outline:2px solid var(--primary-color); outline-offset:3px; border-radius:8px; }
      button:hover { filter:brightness(1.12); }
      .eyebrow { display:flex; align-items:center; gap:7px; font-size:13px; font-weight:600; }
      .eyebrow ha-icon { color:#ffca66; --mdc-icon-size:19px; }
      .top { display:flex; justify-content:space-between; align-items:center; gap:8px; }
      .nav { min-height:28px; color:inherit; opacity:.8; font-size:12px; display:flex; align-items:center; }
      .nav ha-icon { --mdc-icon-size:16px; }
      .hero { display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin:2px 0 4px; }
      .hero strong { font-size:34px; font-weight:650; line-height:1.15; letter-spacing:-1px; font-variant-numeric:tabular-nums; }
      small { font-size:13px; font-weight:400; letter-spacing:0; color:inherit; opacity:.8; }
      .muted { color:inherit; opacity:.8; font-size:12px; line-height:1.5; }
      .floors { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
      .floor { display:flex; align-items:baseline; justify-content:space-between; gap:5px; min-height:24px; }
      .label { display:flex; align-items:center; gap:5px; font-size:12px; color:inherit; opacity:.85; }
      .label ha-icon { --mdc-icon-size:16px; color:#ffca66; }
      .floor:last-child .label ha-icon { color:#ff947d; }
      strong { font-variant-numeric:tabular-nums; white-space:nowrap; }
      .floor strong { font-size:17px; font-weight:600; }
      .detail .hero { margin:22px 0; }
      .detail .hero strong { font-size:48px; }
      .detail .floor { display:flex; flex-direction:column; align-items:flex-start; gap:10px; padding:14px 12px; border:1px solid var(--divider-color); border-radius:12px; }
      .detail .floor strong { font-size:25px; }
      .detail .label { font-size:13px; }
      .grid { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-top:18px; }
      .grid button { display:flex; flex-direction:column; gap:7px; }
      .readings { margin-top:12px; }
      .readings strong { font-size:20px; }
      .readings small { font-size:11px; }
      .readings button { min-height:48px; }
      .energy-list { display:flex; flex-direction:column; gap:0; }
      .energy-list button { display:flex; flex-direction:row; align-items:center; justify-content:space-between; min-height:44px; border-bottom:1px solid var(--divider-color); }
      .energy-list button:last-child { border-bottom:0; }
      .energy-list strong { font-size:24px; }
      .caption { margin-top:5px; font-size:11px; opacity:.8; }
      .note { margin:16px 0 0; font-size:11px; color:inherit; opacity:.8; }
      .meter { display:grid; grid-template-columns:1fr 1fr; gap:24px; align-items:center; }
      .meter .hero { margin-bottom:0; }
      .reference { border-left:1px solid var(--divider-color); padding-left:22px; }
      .reference p { display:flex; justify-content:space-between; gap:12px; margin:8px 0; font-size:13px; }
      @media (max-width:420px) { .meter { grid-template-columns:1fr; gap:8px; } .reference { border-left:0; border-top:1px solid var(--divider-color); padding:8px 0 0; } }
    </style>
    <ha-card class="${detail ? "detail" : ""}">
      ${grid || totals ? `
        <div class="eyebrow"><ha-icon icon="${grid ? "mdi:transmission-tower" : "mdi:counter"}"></ha-icon>${grid ? "Električna mreža" : "Ukupna potrošnja"}</div>
        ${totals ? '<div class="caption">Od početka mjerenja</div>' : ""}
        <div class="grid readings ${totals ? "energy-list" : ""}">
          ${(grid ? [["voltage", "Napon", "V", 1], ["current", "Struja kuće", "A", 2], ["factor", "cos φ", "", 2]]
            : [["energy_house", "Kuća ukupno", "kWh", 2], ["energy_upstairs", "Kat", "kWh", 2], ["energy_ground", "Prizemlje", "kWh", 2]])
            .map(([key, label, unit, digits]) => `<button data-key="${key}"><span class="muted">${label}</span><strong>${this.value(key, unit, digits)}</strong></button>`).join("")}
        </div>` : meter ? `<div class="meter"><div>
        <div class="eyebrow"><ha-icon icon="mdi:counter"></ha-icon>Procjena brojila</div>
        <div class="hero"><strong>${this.format(energy === null || !Number.isFinite(reference) ? null : reference + energy, 2)} <small>kWh</small></strong></div>
        <div class="muted">Izračun prema početnom očitanju</div>
      </div><div class="reference">
        <p><span class="muted">Početno očitanje</span><strong>${this.format(Number.isFinite(reference) ? reference : null, 1)} <small>kWh</small></strong></p>
        <p><span class="muted">Izmjereno od tada</span><strong>+ ${this.format(energy, 2)} <small>kWh</small></strong></p>
        <div class="muted">${escape(this.config.meter_since)}</div>
      </div></div>` : `
      <div class="top"><span class="eyebrow"><ha-icon icon="mdi:lightning-bolt"></ha-icon>${detail ? "Snaga sada" : "Energija · sada"}</span>
        ${detail ? '<span class="muted">Kuća ukupno</span>' : '<button class="nav" data-nav>Detalji <ha-icon icon="mdi:chevron-right"></ha-icon></button>'}
      </div>
      <div class="hero"><button data-key="power_house" aria-label="Trenutačna snaga kuće"><strong>${this.value("power_house", "W")}</strong></button>
        ${detail ? "" : `<span class="muted">${this.value("energy_house", "kWh", 2)}<br>od početka mjerenja</span>`}
      </div>
      <div class="floors">${floor("upstairs", "Kat", "mdi:home-floor-1")}${floor("ground", "Prizemlje", "mdi:home-floor-0")}</div>
      ${detail ? `<div class="grid">
        <button data-key="voltage"><span class="muted">Napon</span><strong>${this.value("voltage", "V", 1)}</strong></button>
        <button data-key="current"><span class="muted">Struja kuće</span><strong>${this.value("current", "A", 2)}</strong></button>
        <button data-key="factor"><span class="muted">Faktor snage</span><strong>${this.value("factor", "", 2)}</strong></button>
      </div><p class="note">Snaga prizemlja izračunava se kao razlika kuće i kata.</p>` : ""}`}
    </ha-card>`;
    this.shadowRoot.querySelectorAll("[data-key]").forEach(button => button.addEventListener("click", () => {
      const entityId = this.config.entities[button.dataset.key];
      if (entityId) this.dispatchEvent(new CustomEvent("hass-more-info", { detail:{ entityId }, bubbles:true, composed:true }));
    }));
    this.shadowRoot.querySelector("[data-nav]")?.addEventListener("click", () => {
      // Keep Overview navigation on Overview, including after mirroring.
      const base = location.pathname.split("/")[1] || "jarvis-dom";
      history.pushState(null, "", `/${base}/potrosnja`);
      window.dispatchEvent(new CustomEvent("location-changed"));
    });
  }
}

if (!customElements.get("jarvis-energy-card")) customElements.define("jarvis-energy-card", JarvisEnergyCard);
window.customCards = window.customCards || [];
window.customCards.push({ type:"jarvis-energy-card", name:"Jarvis energija", description:"Snaga, energija i procjena brojila za zidni panel." });
