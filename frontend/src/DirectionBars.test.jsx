import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import DirectionBars from "./DirectionBars";

describe("direction indicators", () => {
  it("shows independent opposite leans and fills from the center", () => {
    render(<DirectionBars modelProbability={0.2} marketProbability={0.7} />);
    const bot = screen.getByRole("img", { name: "Bot leans: down at 80.0%" });
    const market = screen.getByRole("img", { name: "Market leans: up at 70.0%" });
    expect(bot.querySelector(".direction-fill").style.right).toBe("50%");
    expect(bot.querySelector(".direction-fill").style.width).toBe("30%");
    expect(market.querySelector(".direction-fill").style.left).toBe("50%");
  });

  it("shows a neutral coin flip and the full DOWN probability at zero", () => {
    render(<DirectionBars modelProbability={0.5} marketProbability={0} />);
    expect(screen.getByRole("img", { name: "Bot leans: even at 50.0%" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Market leans: down at 100.0%" })).toBeTruthy();
  });

  it.each([null, undefined, "", "invalid", NaN, Infinity, -0.1, 1.1])("does not invent a direction for %s", (probability) => {
    render(<DirectionBars modelProbability={probability} marketProbability={1} />);
    const bot = screen.getByRole("img", { name: "Bot leans: waiting" });
    expect(bot.querySelector("strong").textContent).toBe("--");
    expect(bot.querySelector(".direction-fill").style.width).toBe("0%");
    expect(screen.getByRole("img", { name: "Market leans: up at 100.0%" })).toBeTruthy();
  });
});
