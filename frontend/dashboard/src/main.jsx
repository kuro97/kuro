import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./style.css";

// basename берём из vite base config — должен совпадать с тем, под каким
// путём nginx раздаёт SPA. Локально (vite dev) BASE_URL = "/", в prod = "/dashboard/".
const basename = import.meta.env.BASE_URL.replace(/\/$/, "");

ReactDOM.createRoot(document.getElementById("root")).render(
  <BrowserRouter basename={basename}>
    <App />
  </BrowserRouter>
);
