import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import { initApp, installTestBridge } from "./store.js";
import "./style.css";

installTestBridge();
createRoot(document.getElementById("root")).render(<App />);
initApp();
