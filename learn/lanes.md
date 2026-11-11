# Learning lanes

The lanes the morning learning briefing watches. scripts/learn/candidates.py
reads this file every morning run; edit it by hand.

Sources were written from memory and were not checked live when this file was
made. A source that fails (moved, gone, not a feed, too slow) is skipped, the
rest of the run carries on, and the failure shows as a warning in the build
job's run log. Fix or remove it here.

Format (strict, one lane per section):

- `## <Lane name>` starts a lane.
- `Watch:` one line saying what the lane is for. It is given to the model as
  context.
- `Keywords:` one line, comma separated. Matched against each item's title and
  summary as whole words with an optional plural s; a keyword in ALL CAPS is
  case sensitive, every other keyword is not. Keep them specific: a keyword
  that matches everything makes every story "on lane".
- `Sources:` followed by one bullet per feed:
  `- <label> | <feed url> | primary`. The label is the name shown for the
  source. The feed must be RSS or Atom over https. Add `| primary` for an
  official lab, vendor, paper or release feed; leave it off for commentary.

Anything above the first lane (like this text) is ignored. A line inside a lane
that does not fit the format is skipped with a warning in the run log.

## Best practices
Watch: How careful teams get reliable results from AI: prompting and context, evals, testing, review and guardrails.
Keywords: best practice, prompt engineering, prompting, prompt, context engineering, system prompt, few-shot, structured output, eval, evals, evaluation, guardrail, red teaming, jailbreak, hallucination, grounding, RAG, retrieval, code review, unit test, regression test, observability, tracing, reliability, pitfall, anti-pattern, when to use
Sources:
- Hamel Husain | https://hamel.dev/index.xml
- Eugene Yan | https://eugeneyan.com/rss/
- Simon Willison | https://simonwillison.net/atom/everything/
- OpenAI news | https://openai.com/news/rss.xml | primary

## Best skills
Watch: Skills worth learning now, for people and for agents: agent skills and plugins, tutorials and courses that teach a real capability.
Keywords: skill, agent skills, plugin, slash command, tutorial, walkthrough, how-to, step-by-step, explainer, primer, getting started, quickstart, course, crash course, masterclass, workshop, curriculum, cookbook, cheat sheet, hands-on, learning path, upskilling, certification
Sources:
- Claude Code releases | https://github.com/anthropics/claude-code/releases.atom | primary
- Hugging Face blog | https://huggingface.co/blog/feed.xml | primary
- Simon Willison | https://simonwillison.net/atom/everything/
- Latent Space | https://www.latent.space/feed

## Best processes
Watch: How teams change the way they work with AI: workflows, playbooks, rollout and governance.
Keywords: workflow, playbook, runbook, checklist, SOP, operating model, process design, standard operating procedure, spec-driven development, pull request, CI/CD, automation, handoff, human in the loop, change management, adoption, rollout, pilot program, case study, onboarding, governance, AI strategy
Sources:
- GitHub blog | https://github.blog/feed/ | primary
- Martin Fowler | https://martinfowler.com/feed.atom
- The Pragmatic Engineer | https://newsletter.pragmaticengineer.com/feed
- Latent Space | https://www.latent.space/feed

## Efficiencies
Watch: Getting more for less: cheaper and faster inference, caching and batching, smaller models, and time saved in real work.
Keywords: inference cost, token cost, cost per token, cost savings, cost reduction, API pricing, token pricing, prompt caching, caching, batch processing, batch API, latency, throughput, tokens per second, speedup, fast mode, distillation, speculative decoding, quantization, pruning, compression, small model, productivity, time savings, time saved
Sources:
- vLLM blog | https://blog.vllm.ai/feed.xml | primary
- NVIDIA technical blog | https://developer.nvidia.com/blog/feed | primary
- arXiv efficient inference | https://export.arxiv.org/api/query?search_query=cat:cs.LG+AND+ti:%22efficient%20inference%22&sortBy=submittedDate&sortOrder=descending&max_results=15 | primary
- AWS machine learning blog | https://aws.amazon.com/blogs/machine-learning/feed/ | primary

## Frontier models
Watch: New and updated frontier models, their system cards, benchmarks and what changed from the last version.
Keywords: frontier model, claude, opus, sonnet, haiku, fable, mythos, gpt, gemini, gemma, llama, mistral, qwen, deepseek, grok, kimi, system card, model card, model release, benchmark, leaderboard, SWE-bench, ARC-AGI, reasoning model, context window, multimodal, openai, anthropic, deepmind, xai, meta ai
Sources:
- OpenAI news | https://openai.com/news/rss.xml | primary
- Google DeepMind blog | https://deepmind.google/blog/rss.xml | primary
- Hugging Face blog | https://huggingface.co/blog/feed.xml | primary
- Interconnects | https://www.interconnects.ai/feed
- Import AI | https://importai.substack.com/feed

## Agents and loops
Watch: Agents that act on their own: tool use, MCP, agent loops, subagents, computer use and coding agents.
Keywords: agent, agentic, tool use, tool calling, function calling, MCP, model context protocol, subagent, multi-agent, agent loop, computer use, browser use, claude code, codex, cursor, copilot, devin, A2A, swarm, sandbox, orchestration
Sources:
- Claude Code releases | https://github.com/anthropics/claude-code/releases.atom | primary
- OpenAI Codex releases | https://github.com/openai/codex/releases.atom | primary
- LangGraph releases | https://github.com/langchain-ai/langgraph/releases.atom | primary
- arXiv agents | https://export.arxiv.org/api/query?search_query=cat:cs.AI+AND+ti:agent&sortBy=submittedDate&sortOrder=descending&max_results=15 | primary
- Latent Space | https://www.latent.space/feed

## Local inference
Watch: Running models on your own hardware: llama.cpp, Ollama, MLX, vLLM, quantized weights and what runs on a laptop or one GPU.
Keywords: local inference, llama.cpp, ollama, gguf, mlx, vllm, lm studio, llamafile, unsloth, local model, local llm, run locally, runs locally, running locally, on-device, edge device, self-hosted, open weights, open-weight, hugging face, apple silicon, mac mini, raspberry pi, consumer GPU, VRAM, NPU, WebGPU, LoRA, quantized, homelab
Sources:
- llama.cpp releases | https://github.com/ggml-org/llama.cpp/releases.atom | primary
- Ollama releases | https://github.com/ollama/ollama/releases.atom | primary
- vLLM releases | https://github.com/vllm-project/vllm/releases.atom | primary
- MLX releases | https://github.com/ml-explore/mlx/releases.atom | primary
- Hugging Face blog | https://huggingface.co/blog/feed.xml | primary

## AI in marketing
Watch: How AI changes marketing work: search and AI answers, ads, content, email and measurement.
Keywords: marketing, marketer, martech, CMO, SEO, GEO, AEO, SERP, PPC, CRM, ai search, search engine, answer engine, AI Overviews, AI Mode, zero-click, organic traffic, advertising, ads, ad campaign, google ads, performance max, content marketing, email marketing, social media, influencer, copywriting, landing page, conversion rate, personalization, lead generation, customer acquisition, brand
Sources:
- Google Ads and Commerce blog | https://blog.google/products/ads-commerce/rss/ | primary
- Search Engine Land | https://searchengineland.com/feed
- Search Engine Journal | https://www.searchenginejournal.com/feed/
- Marketing AI Institute | https://www.marketingaiinstitute.com/blog/rss.xml
