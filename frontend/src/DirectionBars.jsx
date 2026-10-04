import React from "react";
import { percent } from "./utils";

function DirectionBar({ label, probability }) {
  const numeric = Number(probability);
  const valid = probability != null && probability !== "" && Number.isFinite(numeric) && numeric >= 0 && numeric <= 1;
  // Fill grows from the center toward the more likely outcome.
  const lean = valid ? (numeric - 0.5) * 2 : 0;
  const up = lean >= 0;
  const balanced = valid && numeric === 0.5;
  const direction = balanced ? "EVEN" : up ? "UP" : "DOWN";
  const strength = valid ? (up ? numeric : 1 - numeric) : null;
  return <div className="direction" role="img" aria-label={valid ? `${label}: ${direction.toLowerCase()} at ${percent(strength)}` : `${label}: waiting`}>
    <div className="direction-head"><span>{label}</span><strong className={valid && !balanced ? (up ? "up" : "down") : ""}>{valid ? `${direction} \u00b7 ${percent(strength)}` : "--"}</strong></div>
    <div className="direction-track" aria-hidden="true">
      <i>DOWN</i>
      <div className="direction-rail">
        <span className="direction-center" />
        <span className={`direction-fill ${up ? "up" : "down"}`} style={up ? { left: "50%", width: `${Math.abs(lean) * 50}%` } : { right: "50%", width: `${Math.abs(lean) * 50}%` }} />
      </div>
      <i>UP</i>
    </div>
  </div>;
}

export default function DirectionBars({ modelProbability, marketProbability }) {
  return <section className="directions" aria-label="Bot and market direction">
    <DirectionBar label="Bot leans" probability={modelProbability} />
    <DirectionBar label="Market leans" probability={marketProbability} />
  </section>;
}
