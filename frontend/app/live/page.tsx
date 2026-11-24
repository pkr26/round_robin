import type { Metadata } from "next";
import LiveWorkspace from "../live-workspace";
import "../live.css";

export const metadata: Metadata = {
  title: "Round Robin — Your saved decisions",
  description: "Save a comparison, inspect a bounded simulated review and return to the evidence behind a decision.",
};

export default function LivePage() {
  return <LiveWorkspace />;
}
