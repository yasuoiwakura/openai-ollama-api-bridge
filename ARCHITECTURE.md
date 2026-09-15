# Architecture & Historical Diagrams

This document contains architecture diagrams and historical context for the py-ollama-openai-bridge project.

## openai-to-ollama bridge
### Problem Diagram (Historical)

This diagram shows the original problem that motivated the bridge: Ollama's OpenAI-compatible endpoint has serious runtime configuration limitations.

```mermaid
flowchart LR

    subgraph OpenCode
        OC_OpenAI["OpenAI Connector"]
        subgraph JSON["opencode.jsonc"]
            JMAX["max_tokens<br />=64K"]
            JMAXLEN["max response length"]
            J_MODEL["basic model"]
        end

        subgraph OC_POST["POST"]
            OC_COMPLETE["/v1/chat/completions"]
            MODEL["basic model"]
            MSG["messages"]
            MAX["max_tokens<br />=32K"]
            STREAM["stream"]
        end
    end

    subgraph Ollama

        CTX_DEFAULT["Default:<br />num_ctx=4K"]

        subgraph OL_OpenAI["OpenAI Compatibility API"]
            OL_COMPLETE["/v1/chat/completions"]
            OL_MODEL["basic model"]
            CMSG["messages"]
            CMAX["max_tokens<br />=32K"]
            CSTREAM["stream"]
        end

        subgraph Native["Native Ollama APIs"]
            CHAT["POST /api/chat"]
            GEN["POST /api/generate"]
        end

        MODELS[(Any Model **without** Modelfile)]

    end

    J_MODEL --> MODEL

    JMAX -.->|"🔒<br />hardcoded<br />32K"| MAX
    JMAXLEN -.->|"❌<br />ignored"| MAX

    OC_OpenAI -.->|"❌<br />not used"| Native
    OC_COMPLETE -.->|"🚧limited"| OL_COMPLETE

    OC_OpenAI --> OC_POST

    MODEL --> OL_MODEL
    MSG --> CMSG
    MAX --> CMAX
    STREAM --> CSTREAM

    OL_MODEL --Default Model<br/>num_ctx=4K<br/>without Modelfile--> MODELS

    CMSG --> MODELS

    CMAX -.->|"❌<br />ignored"| CTX_DEFAULT

    CTX_DEFAULT -->|"without custom Modelfile"| MODELS

    CSTREAM --> MODELS

    CHAT --> MODELS
    GEN --> MODELS


    style OC_COMPLETE fill:#ffe7aa
    style OL_COMPLETE fill:#ffe7aa
    style CHAT fill:#b5f5b5
    style GEN fill:#b5f5b5
    style JMAX fill:#b5f5b5
    style MAX fill:#ffe7aa
    style CMAX fill:#ffaaaa
    style J_MODEL fill:#b5f5b5
    style OL_MODEL fill:#b5f5b5
    style MODEL fill:#b5f5b5
```

### Parameterfile Workaround (without bridge)

This diagram shows why creating custom Modelfiles for every model was problematic.

```mermaid
flowchart TD

    M1["llama3.2<br/>Model"]
    F11["Base Model<br/>llama3.2"]
    F12["Modelfile<br/>llama3.2<br/>num_ctx=32K"]

    M2["qwen3<br/>Model"]
    F21["Base Model<br/>qwen3"]
    F22["Modelfile<br/>qwen3<br/>num_ctx=32K"]

    M3["gemma3<br/>Model"]
    F31["Base Model<br/>gemma3"]
    F32["Modelfile<br/>gemma3<br/>num_ctx=32K"]

    M4["mistral<br/>Model"]
    F41["Base Model<br/>mistral"]
    F42["Modelfile<br/>mistral<br/>num_ctx=32K"]

    M5["deepseek-r1<br/>Model"]
    F51["Base Model<br/>deepseek-r1"]
    F52["Modelfile<br/>deepseek-r1<br/>num_ctx=32K"]

    M1 --> F11
    M1 --> F12

    M2 --> F21
    M2 --> F22

    M3 --> F31
    M3 --> F32

    M4 --> F41
    M4 --> F42

    M5 --> F51
    M5 --> F52

    style M1 fill:#b5f5b5
    style M2 fill:#b5f5b5
    style M3 fill:#b5f5b5
    style M4 fill:#b5f5b5
    style M5 fill:#b5f5b5

    style F12 fill:#ffe7aa
    style F22 fill:#ffe7aa
    style F32 fill:#ffe7aa
    style F42 fill:#ffe7aa
    style F52 fill:#ffe7aa
```

### translation-mode Default Flow
Central API bridge to enforce runtime settings
Provide a single OpenAI endpoint while centrally enforcing runtime policies and automatically switching to a secondary Ollama server if the primary becomes unavailable.


Provide a single OpenAI endpoint while centrally enforcing runtime policies and automatically switching to a secondary Ollama server if the primary becomes unavailable.

```mermaid
flowchart LR

    Client["OpenCode<br/>OpenAI Connector"]

    subgraph Bridge["API Bridge"]
        TRANS["OpenAI → Ollama<br/>Translation"]
        CONTEXT["Enforce<br/>Context Size Policy"]
        FAILOVER["Switch to other Server"]
    end

    O1["Ollama Server"]

    Client -->|"speaks OpenAI API"| Bridge
    Bridge -->|"speaks Ollama API"| O1

    style Client fill:#ffaaaa
    style Bridge fill:#88FFFF
    style O1 fill:#b5f5b5
```

---

### translation-mode High Availability / Failover

Provide a single OpenAI endpoint while automatically switching to a secondary Ollama server if the primary becomes unavailable.

```mermaid
flowchart LR


    Client["OpenCode<br/>OpenAI Connector"]

    Bridge["OpenAI ↔ Ollama API Bridge"]

    Decision{"Primary Available?"}

    C1["Context Size = 128K"]
    C2["Context Size = 32K"]

    O1["Primary Ollama Server"]
    O2["Secondary Ollama Server"]

    Client --> Bridge
    Bridge --> Decision

    Decision -->|Yes| C1
    Decision -->|No| C2

    C1 --> O1
    C2 --> O2



    style Bridge fill:#ffff88
    style O1 fill:#b5f5b5
    style O2 fill:#b5f5b5
    style C1 fill:#e8f4fd
    style C2 fill:#88FFFF
```

---

### translation-mode Example Setup (Detailed)

This diagram shows a real-world production setup with multiple Ollama servers and infrastructure components.

```mermaid
flowchart TD


    subgraph Laptop
        OC["OpenCode Client"]
    end

    subgraph Homelan["Home LAN"]

        subgraph Router
            IP["Public IPV4"]
            Portforwarding["NAT Forward Port 443"]
        end

        subgraph OptiPlex["Optiplex"]
            subgraph PVE["Proxmox Cluster"]
                subgraph LXC["LXC HomeLab Container"]
                    subgraph Docker["Docker Compose"]
                        ReverseProxy["Traefik/Caddy<br />Reverse Proxy<br/>TLS Termination"]
                        Kong["Kong API Gateway<br />JWT Authorization"]
                        Bridge["API Bridge<br />this project"]
                    end
                end
            end
        end

        subgraph OldLaptop["Linux Laptop"]
            subgraph Debian["Debian Linux"]
                ROCM["Rocm/Vulcan driver"]
                subgraph Debian_Docker["Docker Compose"]
                    ollama_docker["Ollama Server"]
                end
            end
        end

        RX["AMD RX 6900XT<br/>16GB VRAM"]
        subgraph Gaming["Gaming Machine"]
            subgraph Win11["Windows 11"]
                CUDA["CUDA driver"]
                ollama_exe["ollama.exe"]
            end
            RTX["Nvidia RTX 5090<br/>32GB VRAM"]
        end


    end


    OC -->|Internet| IP
    IP --> Portforwarding
    Portforwarding --> ReverseProxy
    ReverseProxy --> Kong
    Kong --> Bridge

    
    Bridge -->|"PRIMARY<br />num_ctx=128K"| ollama_exe

    RTX -.- CUDA
    CUDA -.- ollama_exe

    RX -.-|"Riser Cable"| ROCM
    ROCM -.- ollama_docker

    Bridge -.->|"FAILOVER<br />num_ctx=32K"| ollama_docker
    

    style OptiPlex fill: #88FFFF
    style OldLaptop fill: #88FFFF
    style Gaming fill: #88FFFF
    style Bridge fill:#b5f5b5
```

## Historical Context

These diagrams represent the original problem space and solution approach when the bridge was first conceived. While the core functionality remains the same, the project has evolved to include additional features like:

- **Queue Mode** - Serializing parallel requests for rate-limited APIs
- **Model Name Injection** - `[key=value]` syntax in model names
- **Auto-Pull** - Automatic model downloading on 404 errors
- **Enhanced Failover** - More sophisticated server switching logic

The current architecture is optimized for these newer features, which are now the primary value proposition of the bridge.

## See Also

- [README.md](README.md) - Current project documentation
- [CHANGELOG.md](CHANGELOG.md) - Project evolution history
- [doc/features/](doc/features/) - Detailed feature documentation
