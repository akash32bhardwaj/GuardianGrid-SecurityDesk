/**
 * ResidentPanel.jsx
 * -----------------
 * Complete resident vehicle management UI.
 * Shows resident info when a plate is detected.
 * Allows import from Excel, add/remove residents.
 *
 * Add to App.jsx:
 *   import ResidentPanel from './ResidentPanel';
 *   // In visitors tab or new tab:
 *   <ResidentPanel detectedPlate={alertData?.plate} />
 */

import { useEffect, useState, useRef } from "react";

const API = "http://127.0.0.1:5000";

const STATUS_STYLES = {
  KNOWN:       { bg: "bg-green-900",  text: "text-green-300",  border: "border-green-600",  label: "✅ RESIDENT"   },
  BLACKLISTED: { bg: "bg-red-900",    text: "text-red-300",    border: "border-red-600",    label: "🚫 BLACKLISTED" },
  VISITOR:     { bg: "bg-blue-900",   text: "text-blue-300",   border: "border-blue-600",   label: "👤 VISITOR"    },
  UNKNOWN:     { bg: "bg-gray-900",   text: "text-gray-400",   border: "border-gray-600",   label: "❓ UNKNOWN"    },
};

export default function ResidentPanel({ detectedPlate }) {
  const [residents,    setResidents]    = useState([]);
  const [searchPlate,  setSearchPlate]  = useState("");
  const [lookupResult, setLookupResult] = useState(null);
  const [importing,    setImporting]    = useState(false);
  const [importResult, setImportResult] = useState(null);
  const [adding,       setAdding]       = useState(false);
  const [newResident,  setNewResident]  = useState({
    plate_number:"", resident_name:"", flat_number:"",
    block:"", phone:"", vehicle_type:"Car",
    vehicle_model:"", vehicle_color:"", notes:""
  });
  const fileRef = useRef();

  // Load all residents
  useEffect(() => {
    fetchResidents();
  }, []);

  // Auto-lookup when plate is detected by camera
  useEffect(() => {
    if (detectedPlate) {
      lookupPlate(detectedPlate);
    }
  }, [detectedPlate]);

  async function fetchResidents() {
    try {
      const res  = await fetch(`${API}/residents`);
      const data = await res.json();
      setResidents(data.residents || []);
    } catch {}
  }

  async function lookupPlate(plate) {
    if (!plate) return;
    try {
      const res  = await fetch(`${API}/residents/lookup/${plate}`);
      const data = await res.json();
      setLookupResult(data);
    } catch {}
  }

  async function handleImport(e) {
    const file = e.target.files[0];
    if (!file) return;
    setImporting(true);
    setImportResult(null);
    const formData = new FormData();
    formData.append("file", file);
    try {
      const res  = await fetch(`${API}/residents/import`, {
        method: "POST", body: formData
      });
      const data = await res.json();
      setImportResult(data);
      fetchResidents();
    } catch (err) {
      setImportResult({ error: err.message });
    } finally {
      setImporting(false);
    }
  }

  async function handleAddResident() {
    if (!newResident.plate_number || !newResident.resident_name) return;
    try {
      await fetch(`${API}/residents/add`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(newResident),
      });
      setAdding(false);
      setNewResident({
        plate_number:"", resident_name:"", flat_number:"",
        block:"", phone:"", vehicle_type:"Car",
        vehicle_model:"", vehicle_color:"", notes:""
      });
      fetchResidents();
    } catch {}
  }

  async function handleRemove(plate) {
    if (!window.confirm(`Remove ${plate} from database?`)) return;
    await fetch(`${API}/residents/remove/${plate}`, { method: "DELETE" });
    fetchResidents();
    if (lookupResult?.plate_number === plate) setLookupResult(null);
  }

  async function handleBlacklist(plate) {
    const reason = window.prompt("Reason for blacklisting:");
    if (reason === null) return;
    await fetch(`${API}/residents/blacklist/${plate}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    });
    fetchResidents();
    lookupPlate(plate);
  }

  const sStyle = STATUS_STYLES[lookupResult?.status] || STATUS_STYLES.UNKNOWN;

  return (
    <div>
      <h1 className="text-3xl font-bold text-cyan-400 mb-6">
        🏠 Resident Vehicle Database
      </h1>

      {/* ── Live Detection Card ────────────────────────────── */}
      {detectedPlate && (
        <div className={`${sStyle.bg} ${sStyle.border} border rounded-xl p-5 mb-6`}>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-lg font-bold text-white">📷 Live Detection</h2>
            <span className={`${sStyle.text} font-bold text-sm`}>
              {sStyle.label}
            </span>
          </div>

          <p className={`text-3xl font-mono font-bold ${sStyle.text} mb-3`}>
            {detectedPlate}
          </p>

          {lookupResult?.found ? (
            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-gray-400 text-xs uppercase mb-1">Resident</p>
                <p className="text-white font-semibold text-lg">
                  {lookupResult.resident_name}
                </p>
              </div>
              <div>
                <p className="text-gray-400 text-xs uppercase mb-1">Location</p>
                <p className="text-white font-semibold">
                  Flat {lookupResult.flat_number}
                  {lookupResult.block && ` · Block ${lookupResult.block}`}
                </p>
              </div>
              <div>
                <p className="text-gray-400 text-xs uppercase mb-1">Vehicle</p>
                <p className="text-white">
                  {[lookupResult.vehicle_color, lookupResult.vehicle_model]
                    .filter(Boolean).join(" ") || "—"}
                </p>
              </div>
              <div>
                <p className="text-gray-400 text-xs uppercase mb-1">Phone</p>
                <p className="text-white font-mono">
                  {lookupResult.phone || "—"}
                </p>
              </div>
              {lookupResult.notes && (
                <div className="col-span-2">
                  <p className="text-gray-400 text-xs uppercase mb-1">Notes</p>
                  <p className="text-yellow-300 text-sm">{lookupResult.notes}</p>
                </div>
              )}
            </div>
          ) : (
            <div>
              <p className="text-gray-300 mb-3">
                This vehicle is <strong>not registered</strong> in the system.
              </p>
              <button
                onClick={() => setAdding(true)}
                className="bg-cyan-700 px-4 py-2 rounded text-sm hover:bg-cyan-600"
              >
                ➕ Register This Vehicle
              </button>
            </div>
          )}
        </div>
      )}

      {/* ── Search ────────────────────────────────────────── */}
      <div className="bg-gray-900 border border-gray-700 rounded-xl p-4 mb-6">
        <h2 className="text-lg font-bold text-cyan-300 mb-3">🔍 Search Plate</h2>
        <div className="flex gap-3">
          <input
            placeholder="Enter plate number e.g. PB08EY5332"
            value={searchPlate}
            onChange={e => setSearchPlate(e.target.value)}
            onKeyDown={e => e.key === "Enter" && lookupPlate(searchPlate)}
            className="flex-1 p-3 bg-gray-800 rounded border border-gray-600
                       focus:border-cyan-500 outline-none font-mono"
          />
          <button
            onClick={() => lookupPlate(searchPlate)}
            className="bg-cyan-600 px-6 py-3 rounded hover:bg-cyan-500 font-medium"
          >
            Search
          </button>
        </div>

        {lookupResult && searchPlate && (
          <div className={`mt-4 p-4 rounded-xl border ${sStyle.border} ${sStyle.bg}`}>
            {lookupResult.found ? (
              <div>
                <p className={`font-bold text-lg ${sStyle.text} mb-2`}>
                  {sStyle.label} — {lookupResult.resident_name}
                </p>
                <p className="text-gray-300 text-sm">
                  Flat {lookupResult.flat_number}
                  {lookupResult.block && ` · Block ${lookupResult.block}`}
                  {lookupResult.phone && ` · 📞 ${lookupResult.phone}`}
                </p>
                {lookupResult.vehicle_model && (
                  <p className="text-gray-400 text-sm mt-1">
                    🚗 {lookupResult.vehicle_color} {lookupResult.vehicle_model}
                  </p>
                )}
              </div>
            ) : (
              <p className="text-gray-400">
                ❓ Plate <span className="font-mono text-white">{searchPlate}</span> not found in database
              </p>
            )}
          </div>
        )}
      </div>

      {/* ── Import / Actions ──────────────────────────────── */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        {/* Import Excel */}
        <div className="bg-gray-900 border border-gray-700 rounded-xl p-4">
          <h3 className="font-bold text-cyan-300 mb-2">📥 Import Excel</h3>
          <p className="text-gray-400 text-xs mb-3">
            Upload your residents Excel sheet
          </p>
          <input
            ref={fileRef} type="file"
            accept=".xlsx,.xls,.csv"
            onChange={handleImport}
            className="hidden"
          />
          <button
            onClick={() => fileRef.current.click()}
            disabled={importing}
            className="w-full bg-green-700 py-2 rounded text-sm
                       hover:bg-green-600 disabled:opacity-50"
          >
            {importing ? "Importing..." : "📂 Choose File"}
          </button>
          {importResult && (
            <div className="mt-2 text-xs">
              {importResult.error ? (
                <p className="text-red-400">{importResult.error}</p>
              ) : (
                <p className="text-green-400">
                  ✅ Imported {importResult.imported} · Skipped {importResult.skipped}
                </p>
              )}
            </div>
          )}
        </div>

        {/* Download Template */}
        <div className="bg-gray-900 border border-gray-700 rounded-xl p-4">
          <h3 className="font-bold text-cyan-300 mb-2">📋 Excel Template</h3>
          <p className="text-gray-400 text-xs mb-3">
            Download blank template to fill
          </p>
          <a
            href={`${API}/residents/template`}
            className="block w-full bg-blue-700 py-2 rounded text-sm
                       hover:bg-blue-600 text-center text-white"
          >
            ⬇️ Download Template
          </a>
        </div>

        {/* Add Manually */}
        <div className="bg-gray-900 border border-gray-700 rounded-xl p-4">
          <h3 className="font-bold text-cyan-300 mb-2">➕ Add Manually</h3>
          <p className="text-gray-400 text-xs mb-3">
            Register one vehicle at a time
          </p>
          <button
            onClick={() => setAdding(!adding)}
            className="w-full bg-cyan-700 py-2 rounded text-sm hover:bg-cyan-600"
          >
            {adding ? "✕ Cancel" : "➕ Add Resident"}
          </button>
        </div>
      </div>

      {/* ── Add Resident Form ─────────────────────────────── */}
      {adding && (
        <div className="bg-gray-900 border border-cyan-700 rounded-xl p-5 mb-6">
          <h3 className="font-bold text-cyan-300 mb-4">Register New Vehicle</h3>
          <div className="grid grid-cols-3 gap-3">
            {[
              ["Plate Number *", "plate_number", "font-mono"],
              ["Resident Name *","resident_name",""],
              ["Flat Number",    "flat_number",  ""],
              ["Block / Tower",  "block",        ""],
              ["Phone",          "phone",        ""],
              ["Vehicle Model",  "vehicle_model",""],
              ["Vehicle Color",  "vehicle_color",""],
              ["Notes",          "notes",        ""],
            ].map(([label, key, extra]) => (
              <div key={key}>
                <p className="text-gray-400 text-xs mb-1">{label}</p>
                <input
                  value={newResident[key]}
                  onChange={e => setNewResident({...newResident, [key]: e.target.value})}
                  className={`w-full p-2 bg-gray-800 rounded border border-gray-600
                              focus:border-cyan-500 outline-none text-sm ${extra}`}
                />
              </div>
            ))}
            <div>
              <p className="text-gray-400 text-xs mb-1">Vehicle Type</p>
              <select
                value={newResident.vehicle_type}
                onChange={e => setNewResident({...newResident, vehicle_type: e.target.value})}
                className="w-full p-2 bg-gray-800 rounded border border-gray-600
                           focus:border-cyan-500 outline-none text-sm"
              >
                {["Car","Motorcycle","Bus","Truck"].map(t => (
                  <option key={t}>{t}</option>
                ))}
              </select>
            </div>
          </div>
          <button
            onClick={handleAddResident}
            className="mt-4 bg-green-600 px-8 py-2 rounded hover:bg-green-500 font-medium"
          >
            Save Resident
          </button>
        </div>
      )}

      {/* ── Residents Table ───────────────────────────────── */}
      <div className="bg-gray-900 border border-gray-700 rounded-xl overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3
                        border-b border-gray-700">
          <h3 className="font-bold text-gray-300 uppercase text-sm tracking-widest">
            Registered Vehicles ({residents.length})
          </h3>
          <a
            href={`${API}/residents/export`}
            className="text-xs text-cyan-400 hover:text-cyan-300"
          >
            ⬇️ Export Excel
          </a>
        </div>

        {residents.length === 0 ? (
          <div className="text-center py-12 text-gray-500">
            <p className="text-4xl mb-3">🚗</p>
            <p>No residents registered yet</p>
            <p className="text-sm mt-1">Import an Excel sheet or add manually above</p>
          </div>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-gray-700">
                {["Plate","Resident","Flat","Block","Phone","Vehicle","Status","Actions"]
                  .map(h => (
                  <th key={h} className="text-left px-4 py-2 text-xs
                                         text-gray-500 uppercase">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {residents.map((r, i) => {
                const s = STATUS_STYLES[r.status] || STATUS_STYLES.UNKNOWN;
                return (
                  <tr key={i} className="border-b border-gray-800 hover:bg-gray-800">
                    <td className="px-4 py-3 font-mono text-cyan-300 font-bold text-sm">
                      {r.plate_number}
                    </td>
                    <td className="px-4 py-3 font-medium">{r.resident_name}</td>
                    <td className="px-4 py-3 text-gray-300">{r.flat_number || "—"}</td>
                    <td className="px-4 py-3 text-gray-400">{r.block || "—"}</td>
                    <td className="px-4 py-3 text-gray-400 text-sm font-mono">
                      {r.phone || "—"}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-300">
                      {[r.vehicle_color, r.vehicle_model].filter(Boolean).join(" ") || r.vehicle_type}
                    </td>
                    <td className="px-4 py-3">
                      <span className={`${s.bg} ${s.text} text-xs px-2 py-1
                                        rounded-full font-bold`}>
                        {r.status}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex gap-2">
                        <button
                          onClick={() => handleBlacklist(r.plate_number)}
                          className="text-xs bg-red-900 text-red-300 px-2 py-1
                                     rounded hover:bg-red-800"
                          title="Blacklist"
                        >🚫</button>
                        <button
                          onClick={() => handleRemove(r.plate_number)}
                          className="text-xs bg-gray-700 text-gray-300 px-2 py-1
                                     rounded hover:bg-gray-600"
                          title="Remove"
                        >🗑️</button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
