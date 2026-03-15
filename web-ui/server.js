const express = require("express");
const path = require("path");

const app = express();
const PORT = process.env.PORT || 3000;
const API_URL = process.env.API_URL || "http://mc-api-server-dev:8084";
const AUTH0_DOMAIN = process.env.AUTH0_DOMAIN || "";
const AUTH0_CLIENT_ID = process.env.AUTH0_CLIENT_ID || "";
const AUTH0_AUDIENCE = process.env.AUTH0_AUDIENCE || "";

app.use(express.json());

// Serve Auth0 config so the SPA can initialise without hardcoding
app.get("/auth/config", (_req, res) => {
  res.json({
    domain: AUTH0_DOMAIN,
    clientId: AUTH0_CLIENT_ID,
    audience: AUTH0_AUDIENCE,
  });
});

// Serve static files
app.use(express.static(path.join(__dirname, "public")));

// Proxy API requests to the api-server — forward Authorization header
app.use("/api", async (req, res) => {
  const url = `${API_URL}${req.originalUrl}`;
  const headers = { "Content-Type": "application/json" };
  if (req.headers.authorization) {
    headers["Authorization"] = req.headers.authorization;
  }
  try {
    const resp = await fetch(url, {
      method: req.method,
      headers,
      body: ["POST", "PATCH", "PUT", "DELETE"].includes(req.method)
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
