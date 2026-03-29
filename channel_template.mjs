#!/usr/bin/env node
/**
 * TaskPilot channel server — parameterized version of desktop-channel.
 *
 * Env vars:
 *   TASKPILOT_PORT  — HTTP port (required)
 *   TASKPILOT_NAME  — channel/server name (required)
 *   TASKPILOT_INSTRUCTIONS_FILE — path to instructions markdown (optional)
 *
 * Provides:
 *   - MCP server with claude/channel capability (stdio transport)
 *   - HTTP POST  /         — push message into Claude session
 *   - GET        /events   — SSE stream of replies
 *   - GET        /health   — health check
 */
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'
import http from 'node:http'
import fs from 'node:fs'

const PORT = parseInt(process.env.TASKPILOT_PORT, 10)
const NAME = process.env.TASKPILOT_NAME
if (!PORT || !NAME) {
  process.stderr.write('TASKPILOT_PORT and TASKPILOT_NAME are required\n')
  process.exit(1)
}

// Load instructions if provided
let instructions = [
  `You are an autonomous task agent running as "${NAME}".`,
  'Messages arrive as <channel> notifications. These are directives or responses from your coordinator or the human.',
  'Use the reply tool to send status updates and responses back.',
  'Follow the yessir protocol: never ask to continue, just do it.',
].join('\n')

const instructionsFile = process.env.TASKPILOT_INSTRUCTIONS_FILE
if (instructionsFile && fs.existsSync(instructionsFile)) {
  instructions = fs.readFileSync(instructionsFile, 'utf-8')
}

// --- Outbound: SSE listeners for replies ---
const listeners = new Set()
function send(text) {
  const chunk = text.split('\n').map(l => `data: ${l}\n`).join('') + '\n'
  for (const emit of listeners) emit(chunk)
}

// --- MCP Server with channel capability ---
const mcp = new Server(
  { name: NAME, version: '0.1.0' },
  {
    capabilities: {
      experimental: { 'claude/channel': {} },
      tools: {},
    },
    instructions,
  },
)

// --- Reply tool ---
mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'reply',
      description: 'Send a reply back to the task coordinator or human',
      inputSchema: {
        type: 'object',
        properties: {
          chat_id: {
            type: 'string',
            description: 'The chat_id from the inbound channel tag',
          },
          text: {
            type: 'string',
            description: 'The reply message',
          },
        },
        required: ['chat_id', 'text'],
      },
    },
  ],
}))

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name === 'reply') {
    const { chat_id, text } = req.params.arguments
    send(`[${chat_id}] ${text}`)
    return { content: [{ type: 'text', text: 'sent' }] }
  }
  throw new Error(`unknown tool: ${req.params.name}`)
})

// --- Connect to Claude Code over stdio ---
await mcp.connect(new StdioServerTransport())

// --- HTTP server ---
let nextId = 1

const httpServer = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`)

  // SSE stream for watching replies
  if (req.method === 'GET' && url.pathname === '/events') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    })
    res.write(': connected\n\n')
    const emit = (chunk) => res.write(chunk)
    listeners.add(emit)
    req.on('close', () => listeners.delete(emit))
    return
  }

  // Health check
  if (req.method === 'GET' && url.pathname === '/health') {
    res.writeHead(200)
    res.end('ok')
    return
  }

  // POST: push message into Claude's session
  if (req.method === 'POST') {
    const chunks = []
    for await (const chunk of req) chunks.push(chunk)
    const body = Buffer.concat(chunks).toString()

    const chat_id = String(nextId++)
    await mcp.notification({
      method: 'notifications/claude/channel',
      params: {
        content: body,
        meta: { chat_id, path: url.pathname },
      },
    })
    res.writeHead(200)
    res.end(`ok (chat_id: ${chat_id})`)
    return
  }

  res.writeHead(404)
  res.end('not found')
})

httpServer.listen(PORT, '0.0.0.0', () => {
  // silence — stdout is reserved for MCP stdio transport
})
