const express = require("express");
const path = require("path");

const app = express();
const PORT = process.env.PORT || 3000;
const API_URL = process.env.API_URL || "http://mc-api-server-dev:8084";

app.use(express.json());

// Serve static files
app.use(express.static(path.join(__dirname, "public")));

// Proxy API requests to the api-server so the browser avoids CORS
app.use("/api", async (req, res) => {
  const url = `${API_URL}${req.originalUrl}`;
  try {
    const resp = await fetch(url, {
      method: req.method,
      headers: { "Content-Type": "application/json" },
      body: ["POST", "PATCH", "PUT"].includes(req.method)
        ? JSON.stringify(req.body)
        : undefined,
    });
    const data = await resp.text();
    res.status(resp.status).set("Content-Type", "application/json").send(data);
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

// SPA fallback
app.get("*", (_req, res) => {
  res.sendFile(path.join(__dirname, "public", "index.html"));
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`web-ui listening on :${PORT}`);
});
