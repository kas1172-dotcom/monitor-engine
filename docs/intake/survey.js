/*
 * Intake survey core: pure mapping + validation, no DOM.
 *
 * surveyToIntake(answers) → an intake object matching the schema that
 * tooling/scaffold.py consumes (scaffold.py remains the single mapper from
 * intake → ClientConfig — the survey deliberately does NOT reimplement that).
 * validateAnswers(answers) → array of human-readable error strings ([] = valid).
 *
 * Loadable in the browser (sets window.SurveyIntake) and in Node (module.exports
 * / globalThis.SurveyIntake), so the same logic the form uses is unit-tested.
 */
(function (global) {
  "use strict";

  // Generic, industry-agnostic source library presented as multi-select options.
  // Each selected entry becomes a coverage brief the discovery agent resolves to
  // real sources. "Request other" is captured separately as a free-text brief.
  const SOURCE_LIBRARY = [
    { name: "Federal Register", what: "Proposed and final rules, agency notices", kind: "official", url_hint: "federalregister.gov" },
    { name: "Legislation tracker", what: "Bill introductions and movement", kind: "official", url_hint: "congress.gov / state legislatures" },
    { name: "Regulator press releases", what: "Agency announcements and guidance", kind: "official", url_hint: null },
    { name: "Government contract notices", what: "Solicitations and awards", kind: "official", url_hint: "sam.gov" },
    { name: "Court & enforcement actions", what: "Litigation, settlements, indictments", kind: "official", url_hint: "justice.gov" },
    { name: "SEC filings", what: "Public-company disclosures", kind: "official", url_hint: "sec.gov/edgar" },
    { name: "Company press releases", what: "Announcements from named companies", kind: "primary", url_hint: null },
    { name: "Industry trade press", what: "Sector trade publications", kind: "news", url_hint: null },
    { name: "Major newswires", what: "General business/financial wires", kind: "news", url_hint: null },
    { name: "Standards bodies", what: "Standards and certification updates", kind: "official", url_hint: null },
  ];

  function _list(v) {
    // Accept an array, or a string (newline- or comma-separated); trim + drop blanks.
    if (Array.isArray(v)) return v.map((s) => String(s).trim()).filter(Boolean);
    if (typeof v === "string") return v.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
    return [];
  }

  function _uniq(arr) {
    return Array.from(new Set(arr));
  }

  function _profile(p) {
    p = p || {};
    const ne = p.named_entities || {};
    return {
      capabilities: _list(p.capabilities),
      certifications: _list(p.certifications),
      industries_served: _list(p.industries_served),
      customer_types: _list(p.customer_types),
      geographic_focus: _list(p.geographic_focus),
      strategic_goals: _list(p.strategic_goals),
      risks: _list(p.risks),
      named_entities: {
        customers: _list(ne.customers),
        competitors: _list(ne.competitors),
        agencies: _list(ne.agencies),
        programs: _list(ne.programs),
      },
    };
  }

  function validateAnswers(a) {
    a = a || {};
    const errors = [];
    if (!a.monitor_name || !String(a.monitor_name).trim()) {
      errors.push("Monitor name is required.");
    }
    const editions = (a.editions || []).filter((e) => e && (e.label || "").trim());
    if (editions.length === 0) {
      errors.push("At least one edition with a label is required.");
    }
    editions.forEach((e, i) => {
      if (!(e.role || "").trim()) errors.push(`Edition ${i + 1}: role/audience is required.`);
    });
    const sources = _list(a.sources);
    if (sources.length === 0 && !(a.source_other || "").trim()) {
      errors.push("Select at least one source, or request another.");
    }
    if (!a.cadence || !a.cadence.frequency) {
      errors.push("Cadence frequency is required.");
    }
    return errors;
  }

  function surveyToIntake(a) {
    a = a || {};
    const profile = _profile(a.profile);

    // Industry answer feeds the profile's industries_served (deduped).
    if ((a.industry || "").trim()) {
      profile.industries_served = _uniq([String(a.industry).trim(), ...profile.industries_served]);
    }

    // Editions → audiences. Topics may be array or delimited string.
    const audiences = (a.editions || [])
      .filter((e) => e && (e.label || "").trim())
      .map((e) => ({
        label: String(e.label).trim(),
        role: String(e.role || "").trim(),
        matters: String(e.matters || "").trim(),
        topics: _list(e.topics),
      }));

    // Selected library sources → coverage briefs; "request other" appended.
    const byName = Object.fromEntries(SOURCE_LIBRARY.map((s) => [s.name, s]));
    const coverage = _list(a.sources)
      .map((name) => byName[name])
      .filter(Boolean)
      .map((s) => ({ name: s.name, what: s.what, kind: s.kind, url_hint: s.url_hint }));
    if ((a.source_other || "").trim()) {
      coverage.push({ name: String(a.source_other).trim(), what: "Requested by client", kind: "other", url_hint: null });
    }

    // Top-level named_entities (flat) for the keyword prefilter, derived from the
    // structured profile so the client only enters them once.
    const ne = profile.named_entities;
    const named_entities = _uniq([...ne.customers, ...ne.competitors, ...ne.agencies, ...ne.programs]);

    const intake = {
      monitor_name: String(a.monitor_name || "").trim(),
      audiences,
      must_not_miss: _list(a.must_not_miss),
      noise_to_exclude: _list(a.noise_to_exclude),
      named_entities,
      coverage,
      cadence: {
        frequency: (a.cadence && a.cadence.frequency) || "weekly",
        day: (a.cadence && a.cadence.day) || "monday",
        hour_local: (a.cadence && Number(a.cadence.hour_local)) || 7,
        timezone: (a.cadence && a.cadence.timezone) || "UTC",
      },
      depth_sections: _list(a.depth_sections),
      paid_sources: [],
      profile,
    };
    if ((a.accent_color || "").trim()) intake.accent_color = String(a.accent_color).trim();
    return intake;
  }

  const api = { SOURCE_LIBRARY, surveyToIntake, validateAnswers };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (global) global.SurveyIntake = api;
})(typeof self !== "undefined" ? self : (typeof globalThis !== "undefined" ? globalThis : this));
