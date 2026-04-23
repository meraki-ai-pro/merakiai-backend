# Application Architecture

## Stack

| Component | Technology | Role |
|---|---|---|
| Transport | WebSocket | Persistent client connection throughout chat session |
| Task Broker | RabbitMQ | Task queuing with priority routing and dead letter exchange |
| Result Backend | Redis | Task result storage + Pub/Sub bridge (worker → WebSocket) |
| Worker | Celery | Async AI task execution |
| AI | Model (external/self-hosted) | Processes user prompts |
| DB | — | Task records, chat history, status tracking |

---

## Core Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     WEBSOCKET + CELERY FLOW                                 │
└─────────────────────────────────────────────────────────────────────────────┘

  USER              FRONTEND           BACKEND               REDIS
   │                   │               (WS Manager)            │
   │  Start chat       │                   │                   │
   │──────────────────>│                   │                   │
   │                   │  WS Handshake     │                   │
   │                   │──────────────────>│                   │
   │                   │  Connection open  │                   │
   │                   │<──────────────────│                   │
   │                   │                   │── Register conn ─>│
   │                   │                   │   session:{id}    │
   │                   │                   │                   │
   │                   │                   │── Subscribe ─────>│
   │                   │                   │   ws:{session_id} │
   │                   │                   │                   │
   │  Send message     │                   │                   │
   │──────────────────>│                   │                   │
   │                   │── WS send ───────>│                   │
   │                   │   { message,      │                   │
   │                   │     session_id }  │                   │
   │                   │                   │                   │
   │                   │             RABBITMQ           WORKER (Celery)
   │                   │                   │                   │
   │                   │                   │── Enqueue task ──>│
   │                   │                   │   (with           │── Pick up
   │                   │                   │    session_id,    │   task
   │                   │                   │    priority)      │
   │                   │                   │                   │── Send to AI
   │                   │                   │                   │
   │                   │                   │                   │── Get result
   │                   │                   │                   │
   │                   │                   │                   │── Publish to Redis
   │                   │                   │                   │   ws:{session_id}
   │                   │                   │                   │   { result }
   │                   │                   │                   │
   │                   │                   │<── Redis event ───│
   │                   │                   │    received       │
   │                   │                   │                   │
   │                   │<── WS send ───────│                   │
   │                   │    { result }     │                   │
   │<──────────────────│                   │                   │
   │  Display response │                   │                   │
   │                   │                   │                   │
   │  ... chat continues over same WS connection ...           │


  ON DISCONNECT
  ┌──────────────────────────────────────────────────────────────────┐
  │  User closes chat / drops connection                             │
  │  → Backend removes connection from WS Manager                   │
  │  → Unsubscribes from Redis channel ws:{session_id}              │
  │  → Any pending worker result stored in DB for reconnect         │
  └──────────────────────────────────────────────────────────────────┘
```

---

## Session ID as the Bridge

The `session_id` is the shared key that ties all independent components together.

```
┌─────────────────────────────────────────────────────┐
│              SESSION ID AS THE BRIDGE               │
└─────────────────────────────────────────────────────┘

  Frontend          Backend WS           RabbitMQ            Worker             Redis
     │              Manager                  │                  │                 │
     │  session_id  │                        │                  │                 │
     │─────────────>│                        │                  │                 │
     │              │── key: session_id ─────────────────────────────────────────>│
     │              │   (WS registered)      │                  │                 │
     │              │                        │                  │                 │
     │  message     │                        │                  │                 │
     │─────────────>│                        │                  │                 │
     │              │── task { msg,          │                  │                 │
     │              │         session_id } ─>│                  │                 │
     │              │                        │── task { msg, ──>│                 │
     │              │                        │    session_id }  │                 │
     │              │                        │                  │── publish ─────>│
     │              │                        │                  │   ws:{session_id}
     │              │<── Redis event ────────────────────────────────────────────│
     │              │   (session_id matches) │                  │                 │
     │<─────────────│                        │                  │                 │
     │   result     │                        │                  │                 │


  WHAT SESSION ID CONNECTS:
  ┌──────────────────────────────────────────────────────┐
  │                                                      │
  │   WS Connection  ──┐                                 │
  │                    ├──  session_id  ──┬── Redis ch   │
  │   Celery Task   ───┘                 └── DB record   │
  │                                                      │
  └──────────────────────────────────────────────────────┘
```

---

## RabbitMQ Priority Routing

```
  Premium user task  →  priority: 9  →  dequeued first
  Standard user task →  priority: 5  →  dequeued after premium
  Free user task     →  priority: 1  →  dequeued last
  Failed/expired     →  Dead Letter Exchange → status: failed
```

- Exchange type: **Direct** or **Topic** depending on routing complexity
- `x-max-priority: 10` set on queue declaration
- DLX configured for failed/expired tasks

---

## Horizontal Scaling

```
                    ┌── Instance A (holds user_1 WS connection)
Load Balancer ──────┤
                    └── Instance B (holds user_2 WS connection)

Worker publishes to Redis → Redis broadcasts to ALL instances
Instance A: session_id matches → send over WS
Instance B: session_id not found → ignore
```

Track which instance holds which session in Redis to enable targeted
publishes instead of broadcast under high load.

---

## Performance Bottlenecks

```
┌────────────────────────────────────────────────────────────┐
│  BOTTLENECK          │  IMPACT  │  MITIGATION              │
├──────────────────────┼──────────┼──────────────────────────┤
│  AI model latency    │  CRITICAL│  Stream tokens over WS   │
│  Worker pool size    │  HIGH    │  Autoscale on queue depth │
│  WS horizontal scale │  MEDIUM  │  Target Redis publishes  │
│  Network hops        │  LOW     │  Co-locate services      │
│  Redis Pub/Sub load  │  LOW     │  Only at extreme scale   │
└────────────────────────────────────────────────────────────┘
```

### Network Hop Latency (per message)
```
Frontend → Backend → RabbitMQ → Worker → AI → Worker → Redis → Backend → Frontend
         ~1ms      ~2ms        ~2ms          ~2ms     ~1ms
                                                           Total overhead: ~8ms
                              AI processing: seconds to minutes
```

---

## Infrastructure Roles Summary

```
┌────────────────────────────────────────────────────────────────────┐
│  RabbitMQ  →  Task broker (priority queues, routing, DLX)         │
│  Redis     →  Result backend (task output) + Pub/Sub (WS bridge)  │
│  DB        →  Task records, chat history, status tracking         │
│  Celery    →  Worker orchestration layer                           │
└────────────────────────────────────────────────────────────────────┘
```
