import type { Metadata } from "next";
import DesignWorkspace from "./workspace";
import "../live.css";
import "./visual.css";

export const metadata: Metadata = { title: "Round Robin — Visual workspace", description: "Inspect immutable visual candidates and their exact version-bound review." };

export default function DesignPage() { return <DesignWorkspace />; }
