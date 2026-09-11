# Khayyam AI 🇦🇫

### AI built around Afghan users

**Khayyam AI** is an AI assistant designed specifically for Afghan users. It combines a general-purpose AI model with an Afghan-focused application layer, educational tools, persistent memory, document and image understanding, and locally relevant knowledge.

🌐 **Live:** https://khayyam-ai.onrender.com/
💻 **Repository:** https://github.com/ArakhshQ/khayyam-ai

---

## Why Khayyam?

AI assistants are becoming increasingly powerful, but a general-purpose AI system is not automatically a locally relevant one.

For Afghan users, there can be gaps in:

* Local cultural and historical context
* Afghan educational needs
* Local terminology and expressions
* Afghan literature and poetry
* University and student-related information
* The way people naturally communicate and ask questions
* Educational resources designed around the Afghan context

Khayyam started from a simple question:

> **What would an AI assistant look like if it were designed around the needs and context of Afghan users from the beginning?**

Rather than trying to build another generic chatbot, Khayyam explores how an existing foundation model can be adapted into a more useful and locally relevant AI system.

---

## What Khayyam Can Do

### 💬 AI Chat

Khayyam provides conversational AI designed for Afghan users, with support for local language usage and context.

Users can ask questions, have conversations, request explanations, and use Khayyam as a general-purpose assistant.

### 🧠 Persistent Memory

Khayyam can remember information about a user across conversations.

For example, a user can tell Khayyam something they want it to remember and use that information in future conversations.

### 📚 Personal AI Teacher

Khayyam includes an educational system designed to function more like a personal tutor than a simple chatbot.

Current learning areas include:

* Mathematics
* Science
* English
* Computer Science
* Dari

The learning system includes:

* Level assessment
* Structured lessons
* Multiple difficulty levels
* Quizzes
* Progress tracking
* XP/progress system
* Continuation from previous lessons

### 📄 Document and Image Understanding

Users can provide documents and images and ask Khayyam to explain, analyze, or answer questions about them.

This makes the system useful for students working with textbooks, notes, assignments, and other educational material.

### 📖 Afghan Knowledge and Literature

Khayyam includes locally relevant knowledge and curated material, including Afghan cultural and literary content.

The project includes structured knowledge files that can be expanded as the system develops.

---

## Technical Approach

Khayyam is built as a web application around an existing foundation AI model.

The goal is not to claim that Khayyam is a new language model. Instead, the project focuses on the engineering and research surrounding the model.

The current system includes:

```text
User
  │
  ▼
Khayyam Web Application
  │
  ├── Authentication
  ├── Conversation History
  ├── Persistent Memory
  ├── Educational System
  ├── Knowledge
  ├── Poetry / Literary Content
  ├── Image & Document Handling
  │
  ▼
AI Model
  │
  ▼
Khayyam Response
```

The application is primarily built with Python and Flask, with HTML/CSS/JavaScript on the frontend and a database layer for user and application data.

---

## Project Structure

```text
khayyam-ai/
│
├── app.py              # Main Flask application
├── auth.py             # Authentication functionality
├── database.py         # Database operations
│
├── knowledge.json      # Curated knowledge used by Khayyam
├── poetry.json         # Literary / poetry content
├── examples.json       # Example content
│
├── templates/          # HTML templates
├── static/             # CSS, JavaScript and frontend assets
├── instance/           # Application data
│
├── requirements.txt    # Python dependencies
├── Procfile            # Deployment configuration
└── .gitignore
```

---

# The Research Direction

The current version of Khayyam is a working AI application. The larger goal is to investigate whether an existing general-purpose AI model can be meaningfully adapted for Afghan users.

The next stage of the project is therefore not simply adding more features.

It is **measuring whether localization actually improves the system.**

## 1. Afghan AI Benchmark

I plan to create a benchmark containing Afghan-specific questions across areas such as:

* Afghan history
* Geography
* Education
* Universities
* Literature
* Poetry
* Culture
* Local terminology
* Student life
* Everyday Afghan contexts

The benchmark will be kept separate from Khayyam's knowledge and training data.

The same questions can then be given to different systems and evaluated independently.

For example:

```text
General-purpose model
        ↓
      Test set
        ↓
     Results

Khayyam
        ↓
      Test set
        ↓
     Results
```

This makes it possible to measure whether the Afghan-focused system actually performs better rather than simply assuming that it does.

---

## 2. Afghan Knowledge Base

A larger curated knowledge base will be developed from reliable sources covering Afghan-specific subjects.

Rather than placing all information directly into prompts, the goal is to build a system where Khayyam can retrieve relevant information when answering a question.

Future versions may experiment with retrieval-augmented generation (RAG) and other methods of grounding responses in curated sources.

---

## 3. Fine-Tuning Experiments

Another planned direction is experimenting with fine-tuning.

The goal would not simply be to teach the model a collection of Afghan facts.

Instead, experiments could investigate whether fine-tuning improves:

* Local language usage
* Response style
* Understanding of Afghan context
* Educational explanations
* Consistency
* Ability to handle locally specific questions

Fine-tuned versions would be evaluated against the original model using the same benchmark.

---

## 4. Real User Feedback

Khayyam is intended to be tested with real Afghan users.

User feedback can help identify problems that automated benchmarks cannot fully capture, such as:

* Whether responses feel natural
* Whether explanations are useful to Afghan students
* Whether local context is understood correctly
* Which questions Khayyam consistently gets wrong
* What users actually want from an Afghan-focused AI assistant

The goal is to use this feedback to iteratively improve the system.

---

# From Chatbot to Experiment

The long-term direction of Khayyam is therefore:

```text
General AI model
       │
       ▼
Afghan knowledge
       │
       ▼
Retrieval / context
       │
       ▼
Fine-tuning experiments
       │
       ▼
Khayyam
       │
       ▼
Real Afghan users
       │
       ▼
Evaluation + feedback
       │
       ▼
Improved system
```

The central question is:

> **Can a general-purpose AI model become substantially more useful for a specific community when it is adapted around that community's knowledge, context, language, education, and real-world needs?**

---

# Impact

Khayyam was created with a practical goal: make modern AI more useful and approachable for Afghan users.

Afghanistan has a young population with a large need for accessible educational resources. An AI assistant can potentially provide students with explanations, tutoring, language support, document assistance, and access to information at any time.

However, accessibility is not only about making an AI available.

**Relevance matters too.**

An AI can be globally available while still misunderstanding the context of a particular community.

Khayyam therefore focuses on a different idea:

> **Global AI should not have to mean generic AI.**

The project explores whether AI can be adapted around the needs of a community while still using powerful existing foundation models.

---

# Current Status

Khayyam is currently a working deployed application.

### Current features

* [x] AI chat
* [x] User accounts
* [x] Persistent conversation history
* [x] Persistent memory
* [x] Image uploads
* [x] Document uploads
* [x] Educational system
* [x] Multiple subjects
* [x] Quizzes
* [x] Progress tracking
* [x] Curated local knowledge
* [x] Afghan-focused language/context
* [x] Web deployment

### Planned research

* [ ] Voice interaction
* [ ] Larger Afghan knowledge base
* [ ] Retrieval-augmented generation
* [ ] Afghan AI benchmark
* [ ] Baseline comparison with general-purpose models
* [ ] Real-user evaluation
* [ ] Fine-tuning experiments
* [ ] Quantitative evaluation of improvements

---

# Technology

Current technologies include:

* **Python**
* **Flask**
* **HTML**
* **CSS**
* **JavaScript**
* **SQLite / database systems**
* **OpenAI API**
* **JSON-based knowledge resources**
* **Git / GitHub**
* **Render**

The project intentionally uses an existing foundation model rather than attempting to train a large language model from scratch.

The engineering focus is on building the surrounding system and investigating how effectively an existing model can be adapted for a specific community.

---

# Why the Name "Khayyam"?

The project is named after **Omar Khayyam**, the Persian mathematician, astronomer, philosopher, and poet.

The name represents the connection between computation, knowledge, mathematics, and literature that the project tries to bring together.

---

# Contributing

Khayyam is an evolving project.

Contributions, feedback, ideas, and testing from Afghan users and developers are welcome.

If you find a factual error, cultural misunderstanding, problematic response, or technical issue, please open an issue with enough context to reproduce or evaluate the problem.

---

# Disclaimer

Khayyam uses an external foundation AI model and may produce incorrect or incomplete information.

The project does not claim that every response is accurate, culturally perfect, or authoritative.

Part of the purpose of the project is to identify these limitations, measure them, and investigate ways to improve them.

---

## Author

**Mohammad Arakhsh Qanit**

Built independently as an exploration of AI, software engineering, education, and technology for Afghanistan.

---

### Project Links

* 🌐 **Live Application:** https://khayyam-ai.onrender.com/
* 💻 **GitHub:** https://github.com/ArakhshQ/khayyam-ai


