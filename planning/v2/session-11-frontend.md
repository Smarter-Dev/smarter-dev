# Session 11: Frontend and Landing Page

## Objective
Create a modern, animated landing page with a developer-focused design. Implement interactive elements, smooth animations, and ensure mobile responsiveness while maintaining fast load times.

## Prerequisites
- Completed Session 10 (API complete)
- Understanding of modern CSS and JavaScript
- Familiarity with animation principles

## Task 1: Landing Page HTML

### web/templates/landing.html

Create the main landing page structure:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Join the Smarter Dev community to learn, code, and grow with fellow developers.">
    
    <title>Smarter Dev - Learn. Code. Grow.</title>
    
    <!-- Preload critical fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono&display=swap" as="style">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono&display=swap" rel="stylesheet">
    
    <!-- Styles -->
    <link rel="stylesheet" href="{{ url_for('static', path='/css/landing.css') }}">
    <link rel="stylesheet" href="{{ url_for('static', path='/css/animations.css') }}">
    
    <!-- Favicon -->
    <link rel="icon" type="image/svg+xml" href="{{ url_for('static', path='/img/favicon.svg') }}">
    <link rel="icon" type="image/png" href="{{ url_for('static', path='/img/favicon.png') }}">
</head>
<body>
    <!-- Navigation -->
    <nav class="navbar">
        <div class="container">
            <a href="/" class="logo">
                <span class="logo-text">Smarter</span><span class="logo-dev">.Dev</span>
            </a>
            
            <div class="nav-links">
                <a href="/" class="nav-link">Home</a>
                <a href="/discord" class="nav-link nav-cta">
                    <svg class="nav-icon" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M20.317 4.3698a19.7913 19.7913 0 00-4.8851-1.5152.0741.0741 0 00-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 00-.0785-.037 19.7363 19.7363 0 00-4.8852 1.515.0699.0699 0 00-.0321.0277C.5334 9.0458-.319 13.5799.0992 18.0578a.0824.0824 0 00.0312.0561c2.0528 1.5076 4.0413 2.4228 5.9929 3.0294a.0777.0777 0 00.0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 0 00-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 0 01-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 01.0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 01.0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 01-.0066.1276 12.2986 12.2986 0 01-1.873.8914.0766.0766 0 00-.0407.1067c.3604.698.7719 1.3628 1.225 1.9932a.076.076 0 00.0842.0286c1.961-.6067 3.9495-1.5219 6.0023-3.0294a.077.077 0 00.0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 0 00-.0312-.0286zM8.02 15.3312c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.946 2.4189-2.1568 2.4189Z"/>
                    </svg>
                    Discord
                </a>
            </div>
            
            <button class="mobile-menu-toggle" aria-label="Toggle menu">
                <span></span>
                <span></span>
                <span></span>
            </button>
        </div>
    </nav>
    
    <!-- Hero Section -->
    <section class="hero">
        <div class="container">
            <div class="hero-content">
                <div class="hero-text">
                    <h1 class="hero-title">
                        <span class="fade-in-up">Learn.</span>
                        <span class="fade-in-up delay-1">Code.</span>
                        <span class="fade-in-up delay-2 text-accent">Grow.</span>
                    </h1>
                    
                    <p class="hero-subtitle fade-in-up delay-3">
                        Join the Smarter Dev community and level up your coding skills with fellow developers.
                    </p>
                    
                    <div class="hero-cta fade-in-up delay-4">
                        <a href="/discord" class="btn btn-primary">
                            <svg class="btn-icon" viewBox="0 0 24 24" fill="currentColor">
                                <path d="M20.317 4.3698a19.7913 19.7913 0 00-4.8851-1.5152.0741.0741 0 00-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 00-.0785-.037 19.7363 19.7363 0 00-4.8852 1.515.0699.0699 0 00-.0321.0277C.5334 9.0458-.319 13.5799.0992 18.0578a.0824.0824 0 00.0312.0561c2.0528 1.5076 4.0413 2.4228 5.9929 3.0294a.0777.0777 0 00.0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 0 00-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 0 01-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 01.0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 01.0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 01-.0066.1276 12.2986 12.2986 0 01-1.873.8914.0766.0766 0 00-.0407.1067c.3604.698.7719 1.3628 1.225 1.9932a.076.076 0 00.0842.0286c1.961-.6067 3.9495-1.5219 6.0023-3.0294a.077.077 0 00.0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 0 00-.0312-.0286zM8.02 15.3312c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.946 2.4189-2.1568 2.4189Z"/>
                            </svg>
                            Join Our Discord
                        </a>
                        <a href="#features" class="btn btn-secondary">
                            Learn More
                            <svg class="btn-icon-right" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M7 17l9.2-9.2M17 17V7m0 10H7"/>
                            </svg>
                        </a>
                    </div>
                </div>
                
                <div class="hero-visual">
                    <div class="code-window" id="codeWindow">
                        <div class="code-header">
                            <div class="code-dots">
                                <span class="dot dot-red"></span>
                                <span class="dot dot-yellow"></span>
                                <span class="dot dot-green"></span>
                            </div>
                            <span class="code-title">community.js</span>
                        </div>
                        <pre class="code-content"><code><span class="keyword">const</span> <span class="function">joinCommunity</span> = <span class="keyword">async</span> () => {
  <span class="keyword">const</span> skills = [<span class="string">'coding'</span>, <span class="string">'learning'</span>, <span class="string">'growing'</span>];
  <span class="keyword">const</span> community = <span class="keyword">await</span> <span class="function">connect</span>(<span class="string">'smarter.dev'</span>);
  
  <span class="keyword">return</span> {
    <span class="property">knowledge</span>: skills.<span class="function">map</span>(s => s + <span class="string">'+++'</span>),
    <span class="property">friends</span>: <span class="keyword">true</span>,
    <span class="property">fun</span>: <span class="number">100</span>
  };
};</code></pre>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Animated background shapes -->
        <div class="hero-bg-shapes">
            <div class="shape shape-1"></div>
            <div class="shape shape-2"></div>
            <div class="shape shape-3"></div>
        </div>
    </section>
    
    <!-- Features Section -->
    <section class="features" id="features">
        <div class="container">
            <div class="section-header">
                <h2 class="section-title">What is <span class="text-gradient">Smarter.Dev</span>?</h2>
                <div class="section-underline"></div>
            </div>
            
            <div class="features-grid">
                <!-- Community Learning -->
                <div class="feature-card fade-in-up">
                    <div class="feature-icon">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <rect x="2" y="7" width="20" height="14" rx="2" ry="2"></rect>
                            <path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"></path>
                        </svg>
                    </div>
                    <h3 class="feature-title">Community Learning</h3>
                    <p class="feature-description">
                        Connect with developers of all skill levels to learn, share knowledge, and grow together.
                    </p>
                </div>
                
                <!-- Coding Challenges -->
                <div class="feature-card fade-in-up delay-1">
                    <div class="feature-icon">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="16 18 22 12 16 6"></polyline>
                            <polyline points="8 6 2 12 8 18"></polyline>
                        </svg>
                    </div>
                    <h3 class="feature-title">Coding Challenges</h3>
                    <p class="feature-description">
                        Sharpen your skills with regular coding challenges and collaborative problem-solving.
                    </p>
                </div>
                
                <!-- Project Collaboration -->
                <div class="feature-card fade-in-up delay-2">
                    <div class="feature-icon">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>
                            <polyline points="2 17 12 22 22 17"></polyline>
                            <polyline points="2 12 12 17 22 12"></polyline>
                        </svg>
                    </div>
                    <h3 class="feature-title">Project Collaboration</h3>
                    <p class="feature-description">
                        Work on real projects with other community members to build your portfolio and experience.
                    </p>
                </div>
            </div>
        </div>
    </section>
    
    <!-- CTA Section -->
    <section class="cta">
        <div class="container">
            <div class="cta-content">
                <h2 class="cta-title">Ready to join our community?</h2>
                <p class="cta-subtitle">
                    Connect with fellow developers, share knowledge, and level up your coding skills.
                </p>
                <a href="/discord" class="btn btn-primary btn-large">
                    <svg class="btn-icon" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M20.317 4.3698a19.7913 19.7913 0 00-4.8851-1.5152.0741.0741 0 00-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 00-.0785-.037 19.7363 19.7363 0 00-4.8852 1.515.0699.0699 0 00-.0321.0277C.5334 9.0458-.319 13.5799.0992 18.0578a.0824.0824 0 00.0312.0561c2.0528 1.5076 4.0413 2.4228 5.9929 3.0294a.0777.0777 0 00.0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 0 00-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 0 01-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 01.0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 01.0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 01-.0066.1276 12.2986 12.2986 0 01-1.873.8914.0766.0766 0 00-.0407.1067c.3604.698.7719 1.3628 1.225 1.9932a.076.076 0 00.0842.0286c1.961-.6067 3.9495-1.5219 6.0023-3.0294a.077.077 0 00.0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 0 00-.0312-.0286zM8.02 15.3312c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.946 2.4189-2.1568 2.4189Z"/>
                    </svg>
                    Join Discord Server
                </a>
            </div>
        </div>
        
        <div class="cta-bg-gradient"></div>
    </section>
    
    <!-- Footer -->
    <footer class="footer">
        <div class="container">
            <div class="footer-content">
                <div class="footer-brand">
                    <a href="/" class="logo">
                        <span class="logo-text">Smarter</span><span class="logo-dev">.Dev</span>
                    </a>
                    <p class="footer-tagline">Learn. Code. Grow.</p>
                </div>
                
                <div class="footer-links">
                    <h4 class="footer-title">Connect</h4>
                    <a href="/discord" class="footer-link">Discord</a>
                    <a href="https://github.com/smarter-dev" class="footer-link">GitHub</a>
                    <a href="mailto:hello@smarter.dev" class="footer-link">Contact</a>
                </div>
            </div>
            
            <div class="footer-bottom">
                <p>&copy; 2024 Smarter.Dev. All rights reserved.</p>
            </div>
        </div>
    </footer>
    
    <!-- Scripts -->
    <script src="{{ url_for('static', path='/js/landing.js') }}" defer></script>
</body>
</html>
```

## Task 2: Landing Page CSS

### web/static/css/landing.css

Main landing page styles:

```css
/* CSS Variables */
:root {
    --color-background: #1a1a1a;
    --color-surface: #242424;
    --color-surface-light: #2a2a2a;
    --color-primary: #3b82f6;
    --color-primary-dark: #2563eb;
    --color-accent: #22c55e;
    --color-accent-dark: #16a34a;
    --color-text: #e2e8f0;
    --color-text-muted: #94a3b8;
    --color-border: #334155;
    
    --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    --font-mono: 'JetBrains Mono', 'Courier New', monospace;
    
    --radius: 8px;
    --radius-lg: 12px;
    
    --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.05);
    --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
    --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1);
    --shadow-glow: 0 0 20px rgba(59, 130, 246, 0.5);
}

/* Reset & Base */
*, *::before, *::after {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

html {
    scroll-behavior: smooth;
}

body {
    font-family: var(--font-sans);
    font-size: 16px;
    line-height: 1.6;
    color: var(--color-text);
    background-color: var(--color-background);
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}

/* Container */
.container {
    width: 100%;
    max-width: 1200px;
    margin: 0 auto;
    padding: 0 20px;
}

/* Navigation */
.navbar {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    z-index: 1000;
    background: rgba(26, 26, 26, 0.8);
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--color-border);
    transition: all 0.3s ease;
}

.navbar .container {
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 64px;
}

.logo {
    text-decoration: none;
    font-size: 1.5rem;
    font-weight: 700;
    display: flex;
    align-items: center;
}

.logo-text {
    color: var(--color-text);
}

.logo-dev {
    color: var(--color-accent);
}

.nav-links {
    display: flex;
    align-items: center;
    gap: 2rem;
}

.nav-link {
    color: var(--color-text-muted);
    text-decoration: none;
    font-weight: 500;
    transition: color 0.3s ease;
}

.nav-link:hover {
    color: var(--color-text);
}

.nav-cta {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 1rem;
    background: var(--color-primary);
    color: white;
    border-radius: var(--radius);
    transition: all 0.3s ease;
}

.nav-cta:hover {
    background: var(--color-primary-dark);
    transform: translateY(-1px);
    box-shadow: var(--shadow-glow);
}

.nav-icon {
    width: 20px;
    height: 20px;
}

.mobile-menu-toggle {
    display: none;
    flex-direction: column;
    gap: 4px;
    background: none;
    border: none;
    cursor: pointer;
    padding: 8px;
}

.mobile-menu-toggle span {
    width: 24px;
    height: 2px;
    background: var(--color-text);
    transition: all 0.3s ease;
}

/* Hero Section */
.hero {
    position: relative;
    min-height: 100vh;
    display: flex;
    align-items: center;
    padding: 100px 0 80px;
    overflow: hidden;
}

.hero-content {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 60px;
    align-items: center;
}

.hero-title {
    font-size: clamp(2.5rem, 5vw, 4rem);
    font-weight: 700;
    line-height: 1.1;
    margin-bottom: 1.5rem;
}

.hero-title span {
    display: inline-block;
}

.text-accent {
    color: var(--color-accent);
}

.hero-subtitle {
    font-size: 1.25rem;
    color: var(--color-text-muted);
    margin-bottom: 2rem;
    max-width: 500px;
}

.hero-cta {
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
}

/* Buttons */
.btn {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.75rem 1.5rem;
    font-weight: 600;
    text-decoration: none;
    border-radius: var(--radius);
    transition: all 0.3s ease;
    cursor: pointer;
    border: none;
    font-size: 1rem;
}

.btn-primary {
    background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
    color: white;
    box-shadow: var(--shadow-md);
}

.btn-primary:hover {
    transform: translateY(-2px);
    box-shadow: var(--shadow-lg), var(--shadow-glow);
}

.btn-secondary {
    background: rgba(255, 255, 255, 0.05);
    color: var(--color-text);
    border: 1px solid var(--color-border);
}

.btn-secondary:hover {
    background: rgba(255, 255, 255, 0.1);
    border-color: var(--color-primary);
}

.btn-large {
    padding: 1rem 2rem;
    font-size: 1.125rem;
}

.btn-icon {
    width: 20px;
    height: 20px;
}

.btn-icon-right {
    width: 16px;
    height: 16px;
}

/* Code Window */
.code-window {
    background: var(--color-surface);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-lg);
    overflow: hidden;
    transform: perspective(1000px) rotateY(-5deg) rotateX(5deg);
    transition: transform 0.3s ease;
}

.code-window:hover {
    transform: perspective(1000px) rotateY(0) rotateX(0);
}

.code-header {
    background: var(--color-surface-light);
    padding: 12px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}

.code-dots {
    display: flex;
    gap: 8px;
}

.dot {
    width: 12px;
    height: 12px;
    border-radius: 50%;
}

.dot-red {
    background: #ff5f56;
}

.dot-yellow {
    background: #ffbd2e;
}

.dot-green {
    background: #27c93f;
}

.code-title {
    font-family: var(--font-mono);
    font-size: 0.875rem;
    color: var(--color-text-muted);
}

.code-content {
    padding: 24px;
    font-family: var(--font-mono);
    font-size: 0.875rem;
    line-height: 1.6;
    overflow-x: auto;
}

.code-content code {
    display: block;
}

/* Syntax Highlighting */
.keyword {
    color: #ff79c6;
}

.function {
    color: #50fa7b;
}

.string {
    color: #f1fa8c;
}

.property {
    color: #8be9fd;
}

.number {
    color: #bd93f9;
}

/* Background Shapes */
.hero-bg-shapes {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    overflow: hidden;
    pointer-events: none;
    z-index: -1;
}

.shape {
    position: absolute;
    border-radius: 50%;
    filter: blur(40px);
    opacity: 0.3;
}

.shape-1 {
    width: 400px;
    height: 400px;
    background: var(--color-primary);
    top: -200px;
    right: -100px;
    animation: float 20s ease-in-out infinite;
}

.shape-2 {
    width: 300px;
    height: 300px;
    background: var(--color-accent);
    bottom: -150px;
    left: -100px;
    animation: float 15s ease-in-out infinite reverse;
}

.shape-3 {
    width: 200px;
    height: 200px;
    background: var(--color-primary);
    top: 50%;
    left: 50%;
    animation: float 25s ease-in-out infinite;
}

/* Features Section */
.features {
    padding: 80px 0;
    background: var(--color-surface);
}

.section-header {
    text-align: center;
    margin-bottom: 60px;
}

.section-title {
    font-size: clamp(2rem, 4vw, 3rem);
    font-weight: 700;
    margin-bottom: 1rem;
}

.text-gradient {
    background: linear-gradient(135deg, var(--color-primary), var(--color-accent));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}

.section-underline {
    width: 80px;
    height: 4px;
    background: linear-gradient(90deg, var(--color-primary), var(--color-accent));
    margin: 0 auto;
    border-radius: 2px;
}

.features-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 40px;
}

.feature-card {
    background: var(--color-background);
    padding: 40px;
    border-radius: var(--radius-lg);
    text-align: center;
    transition: all 0.3s ease;
    border: 1px solid transparent;
}

.feature-card:hover {
    transform: translateY(-5px);
    border-color: var(--color-primary);
    box-shadow: var(--shadow-lg);
    background: linear-gradient(135deg, 
        rgba(59, 130, 246, 0.05), 
        rgba(34, 197, 94, 0.05)
    );
}

.feature-icon {
    width: 60px;
    height: 60px;
    margin: 0 auto 20px;
    color: var(--color-primary);
}

.feature-card:nth-child(2) .feature-icon {
    color: var(--color-accent);
}

.feature-card:nth-child(3) .feature-icon {
    color: var(--color-primary);
}

.feature-title {
    font-size: 1.5rem;
    font-weight: 600;
    margin-bottom: 1rem;
}

.feature-description {
    color: var(--color-text-muted);
    line-height: 1.8;
}

/* CTA Section */
.cta {
    position: relative;
    padding: 100px 0;
    text-align: center;
    overflow: hidden;
}

.cta-content {
    position: relative;
    z-index: 1;
}

.cta-title {
    font-size: clamp(2rem, 4vw, 3rem);
    font-weight: 700;
    margin-bottom: 1rem;
}

.cta-subtitle {
    font-size: 1.25rem;
    color: var(--color-text-muted);
    margin-bottom: 2rem;
    max-width: 600px;
    margin-left: auto;
    margin-right: auto;
}

.cta-bg-gradient {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: radial-gradient(
        circle at center,
        rgba(59, 130, 246, 0.1) 0%,
        transparent 70%
    );
    pointer-events: none;
}

/* Footer */
.footer {
    background: var(--color-surface);
    padding: 60px 0 20px;
    border-top: 1px solid var(--color-border);
}

.footer-content {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 40px;
    margin-bottom: 40px;
}

.footer-tagline {
    color: var(--color-text-muted);
    margin-top: 0.5rem;
}

.footer-title {
    font-size: 0.875rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--color-text-muted);
    margin-bottom: 1rem;
}

.footer-links {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
}

.footer-link {
    color: var(--color-text);
    text-decoration: none;
    transition: color 0.3s ease;
}

.footer-link:hover {
    color: var(--color-primary);
}

.footer-bottom {
    padding-top: 20px;
    border-top: 1px solid var(--color-border);
    text-align: center;
    color: var(--color-text-muted);
    font-size: 0.875rem;
}

/* Responsive */
@media (max-width: 768px) {
    .nav-links {
        display: none;
    }
    
    .mobile-menu-toggle {
        display: flex;
    }
    
    .hero-content {
        grid-template-columns: 1fr;
        text-align: center;
    }
    
    .hero-text {
        order: 2;
    }
    
    .hero-visual {
        order: 1;
        max-width: 400px;
        margin: 0 auto;
    }
    
    .hero-cta {
        justify-content: center;
    }
    
    .code-window {
        transform: none;
    }
    
    .features-grid {
        grid-template-columns: 1fr;
        gap: 30px;
    }
    
    .footer-content {
        grid-template-columns: 1fr;
        text-align: center;
    }
    
    .footer-links {
        align-items: center;
    }
}

@media (max-width: 480px) {
    .hero {
        padding: 80px 0 60px;
    }
    
    .hero-title {
        font-size: 2rem;
    }
    
    .hero-subtitle {
        font-size: 1.125rem;
    }
    
    .btn {
        width: 100%;
        justify-content: center;
    }
    
    .feature-card {
        padding: 30px 20px;
    }
}
```

## Task 3: Animation Styles

### web/static/css/animations.css

Reusable animation classes:

```css
/* Keyframes */
@keyframes fadeInUp {
    from {
        opacity: 0;
        transform: translateY(30px);
    }
    to {
        opacity: 1;
        transform: translateY(0);
    }
}

@keyframes fadeIn {
    from {
        opacity: 0;
    }
    to {
        opacity: 1;
    }
}

@keyframes float {
    0%, 100% {
        transform: translate(0, 0) scale(1);
    }
    33% {
        transform: translate(30px, -30px) scale(1.05);
    }
    66% {
        transform: translate(-20px, 20px) scale(0.95);
    }
}

@keyframes pulse {
    0%, 100% {
        opacity: 1;
    }
    50% {
        opacity: 0.7;
    }
}

@keyframes slideInRight {
    from {
        opacity: 0;
        transform: translateX(30px);
    }
    to {
        opacity: 1;
        transform: translateX(0);
    }
}

@keyframes slideInLeft {
    from {
        opacity: 0;
        transform: translateX(-30px);
    }
    to {
        opacity: 1;
        transform: translateX(0);
    }
}

@keyframes scaleIn {
    from {
        opacity: 0;
        transform: scale(0.9);
    }
    to {
        opacity: 1;
        transform: scale(1);
    }
}

/* Animation Classes */
.fade-in-up {
    opacity: 0;
    animation: fadeInUp 0.8s ease forwards;
}

.fade-in {
    opacity: 0;
    animation: fadeIn 0.8s ease forwards;
}

.slide-in-right {
    opacity: 0;
    animation: slideInRight 0.8s ease forwards;
}

.slide-in-left {
    opacity: 0;
    animation: slideInLeft 0.8s ease forwards;
}

.scale-in {
    opacity: 0;
    animation: scaleIn 0.8s ease forwards;
}

/* Delays */
.delay-1 {
    animation-delay: 0.1s;
}

.delay-2 {
    animation-delay: 0.2s;
}

.delay-3 {
    animation-delay: 0.3s;
}

.delay-4 {
    animation-delay: 0.4s;
}

.delay-5 {
    animation-delay: 0.5s;
}

/* Scroll Animations */
.animate-on-scroll {
    opacity: 0;
    transform: translateY(30px);
    transition: all 0.8s ease;
}

.animate-on-scroll.animated {
    opacity: 1;
    transform: translateY(0);
}

/* Loading States */
.skeleton {
    background: linear-gradient(
        90deg,
        var(--color-surface) 25%,
        var(--color-surface-light) 50%,
        var(--color-surface) 75%
    );
    background-size: 200% 100%;
    animation: loading 1.5s infinite;
}

@keyframes loading {
    0% {
        background-position: 200% 0;
    }
    100% {
        background-position: -200% 0;
    }
}

/* Hover Effects */
.hover-lift {
    transition: transform 0.3s ease, box-shadow 0.3s ease;
}

.hover-lift:hover {
    transform: translateY(-5px);
    box-shadow: var(--shadow-lg);
}

.hover-glow {
    transition: all 0.3s ease;
}

.hover-glow:hover {
    box-shadow: var(--shadow-glow);
}

/* Reduced Motion */
@media (prefers-reduced-motion: reduce) {
    *,
    *::before,
    *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
    }
}
```

## Task 4: Landing Page JavaScript

### web/static/js/landing.js

Interactive functionality:

```javascript
// Landing Page JavaScript

class LandingPage {
    constructor() {
        this.init();
    }

    init() {
        this.setupMobileMenu();
        this.setupScrollAnimations();
        this.setupCodeWindow3D();
        this.setupSmoothScroll();
        this.setupNavbarScroll();
    }

    // Mobile Menu Toggle
    setupMobileMenu() {
        const toggle = document.querySelector('.mobile-menu-toggle');
        const navLinks = document.querySelector('.nav-links');
        
        if (!toggle || !navLinks) return;

        toggle.addEventListener('click', () => {
            navLinks.classList.toggle('active');
            toggle.classList.toggle('active');
        });

        // Close menu on link click
        navLinks.querySelectorAll('.nav-link').forEach(link => {
            link.addEventListener('click', () => {
                navLinks.classList.remove('active');
                toggle.classList.remove('active');
            });
        });
    }

    // Scroll Animations
    setupScrollAnimations() {
        const animatedElements = document.querySelectorAll('.animate-on-scroll');
        
        if (!animatedElements.length) return;

        const observer = new IntersectionObserver(
            (entries) => {
                entries.forEach(entry => {
                    if (entry.isIntersecting) {
                        entry.target.classList.add('animated');
                    }
                });
            },
            {
                threshold: 0.1,
                rootMargin: '0px 0px -50px 0px'
            }
        );

        animatedElements.forEach(el => observer.observe(el));
    }

    // 3D Code Window Effect
    setupCodeWindow3D() {
        const codeWindow = document.getElementById('codeWindow');
        if (!codeWindow) return;

        let mouseX = 0;
        let mouseY = 0;
        let targetX = 0;
        let targetY = 0;

        const handleMouseMove = (e) => {
            const rect = codeWindow.getBoundingClientRect();
            const centerX = rect.left + rect.width / 2;
            const centerY = rect.top + rect.height / 2;

            mouseX = (e.clientX - centerX) / rect.width;
            mouseY = (e.clientY - centerY) / rect.height;
        };

        const animate = () => {
            targetX += (mouseX - targetX) * 0.1;
            targetY += (mouseY - targetY) * 0.1;

            const rotateY = targetX * 15;
            const rotateX = targetY * -15;

            codeWindow.style.transform = `
                perspective(1000px)
                rotateY(${rotateY}deg)
                rotateX(${rotateX}deg)
                scale(1.05)
            `;

            requestAnimationFrame(animate);
        };

        // Only apply effect on desktop
        if (window.innerWidth > 768) {
            document.addEventListener('mousemove', handleMouseMove);
            animate();
        }

        // Clean up on mobile
        window.addEventListener('resize', () => {
            if (window.innerWidth <= 768) {
                document.removeEventListener('mousemove', handleMouseMove);
                codeWindow.style.transform = 'none';
            }
        });
    }

    // Smooth Scroll
    setupSmoothScroll() {
        document.querySelectorAll('a[href^="#"]').forEach(anchor => {
            anchor.addEventListener('click', function (e) {
                e.preventDefault();
                
                const targetId = this.getAttribute('href');
                if (targetId === '#') return;
                
                const target = document.querySelector(targetId);
                if (!target) return;

                const navHeight = document.querySelector('.navbar').offsetHeight;
                const targetPosition = target.getBoundingClientRect().top + window.pageYOffset - navHeight;

                window.scrollTo({
                    top: targetPosition,
                    behavior: 'smooth'
                });
            });
        });
    }

    // Navbar Scroll Effect
    setupNavbarScroll() {
        const navbar = document.querySelector('.navbar');
        if (!navbar) return;

        let lastScroll = 0;

        window.addEventListener('scroll', () => {
            const currentScroll = window.pageYOffset;

            if (currentScroll > 100) {
                navbar.classList.add('scrolled');
            } else {
                navbar.classList.remove('scrolled');
            }

            // Hide/show on scroll
            if (currentScroll > lastScroll && currentScroll > 300) {
                navbar.classList.add('hidden');
            } else {
                navbar.classList.remove('hidden');
            }

            lastScroll = currentScroll;
        });
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    new LandingPage();
});

// Add navbar scroll styles dynamically
const style = document.createElement('style');
style.textContent = `
    .navbar.scrolled {
        background: rgba(26, 26, 26, 0.95);
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
    }
    
    .navbar.hidden {
        transform: translateY(-100%);
    }
    
    @media (max-width: 768px) {
        .nav-links {
            position: fixed;
            top: 64px;
            left: 0;
            right: 0;
            background: var(--color-background);
            padding: 20px;
            border-bottom: 1px solid var(--color-border);
            transform: translateY(-100%);
            opacity: 0;
            transition: all 0.3s ease;
        }
        
        .nav-links.active {
            transform: translateY(0);
            opacity: 1;
        }
        
        .nav-links {
            flex-direction: column;
            align-items: stretch;
            gap: 1rem;
        }
        
        .nav-cta {
            width: 100%;
            justify-content: center;
        }
        
        .mobile-menu-toggle.active span:nth-child(1) {
            transform: rotate(45deg) translate(5px, 5px);
        }
        
        .mobile-menu-toggle.active span:nth-child(2) {
            opacity: 0;
        }
        
        .mobile-menu-toggle.active span:nth-child(3) {
            transform: rotate(-45deg) translate(7px, -6px);
        }
    }
`;
document.head.appendChild(style);
```

## Task 5: Static Assets

### web/static/img/favicon.svg

Create a simple SVG favicon:

```svg
<svg width="32" height="32" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
    <rect width="32" height="32" rx="8" fill="#1a1a1a"/>
    <path d="M8 20V12C8 10.8954 8.89543 10 10 10H14C15.1046 10 16 10.8954 16 12V20" stroke="#3b82f6" stroke-width="2" stroke-linecap="round"/>
    <path d="M16 16H20C21.1046 16 22 16.8954 22 18V20" stroke="#22c55e" stroke-width="2" stroke-linecap="round"/>
    <circle cx="20" cy="12" r="2" fill="#22c55e"/>
</svg>
```

### web/static/js/components/CodeWindow.js

Reusable code window component:

```javascript
// Code Window Component

class CodeWindow {
    constructor(element, options = {}) {
        this.element = element;
        this.options = {
            language: 'javascript',
            theme: 'dark',
            animated: true,
            ...options
        };
        
        this.init();
    }

    init() {
        this.render();
        
        if (this.options.animated) {
            this.setupAnimation();
        }
    }

    render() {
        const code = this.element.textContent.trim();
        const highlighted = this.highlightCode(code);
        
        this.element.innerHTML = `
            <div class="code-window">
                <div class="code-header">
                    <div class="code-dots">
                        <span class="dot dot-red"></span>
                        <span class="dot dot-yellow"></span>
                        <span class="dot dot-green"></span>
                    </div>
                    <span class="code-title">${this.options.filename || 'code.js'}</span>
                </div>
                <pre class="code-content"><code>${highlighted}</code></pre>
            </div>
        `;
    }

    highlightCode(code) {
        // Simple syntax highlighting
        return code
            .replace(/\/\/.*/g, '<span class="comment">$&</span>')
            .replace(/(['"`])([^'"`]*)(['"`])/g, '<span class="string">$1$2$3</span>')
            .replace(/\b(const|let|var|function|async|await|return|if|else|for|while|class|import|export|from|new|this)\b/g, '<span class="keyword">$1</span>')
            .replace(/\b(true|false|null|undefined)\b/g, '<span class="boolean">$1</span>')
            .replace(/\b(\d+)\b/g, '<span class="number">$1</span>')
            .replace(/([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\(/g, '<span class="function">$1</span>(');
    }

    setupAnimation() {
        const lines = this.element.querySelectorAll('.code-content > code > *');
        
        lines.forEach((line, index) => {
            line.style.opacity = '0';
            line.style.transform = 'translateX(-10px)';
            
            setTimeout(() => {
                line.style.transition = 'all 0.3s ease';
                line.style.opacity = '1';
                line.style.transform = 'translateX(0)';
            }, index * 100);
        });
    }
}

// Auto-initialize code windows
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-code-window]').forEach(element => {
        new CodeWindow(element, {
            filename: element.dataset.filename,
            language: element.dataset.language,
            animated: element.dataset.animated !== 'false'
        });
    });
});
```

## Task 6: Performance Optimization

### web/pages/public.py

Optimize landing page delivery:

```python
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles
import hashlib
from datetime import datetime, timedelta

templates = Jinja2Templates(directory="web/templates")

# Cache static asset versions
ASSET_VERSION = hashlib.md5(
    f"{datetime.now().isoformat()}".encode()
).hexdigest()[:8]

async def home_page(request: Request) -> HTMLResponse:
    """Landing page with optimizations."""
    response = templates.TemplateResponse(
        "landing.html",
        {
            "request": request,
            "asset_version": ASSET_VERSION,
            "preload_fonts": True,
            "enable_analytics": not request.app.state.config.dev_mode
        }
    )
    
    # Add performance headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    
    # Cache for 1 hour in production
    if not request.app.state.config.dev_mode:
        response.headers["Cache-Control"] = "public, max-age=3600"
    
    return response

async def discord_redirect(request: Request) -> RedirectResponse:
    """Redirect to Discord invite."""
    # Track the click if needed
    logger.info(
        "Discord redirect",
        referrer=request.headers.get("referer"),
        user_agent=request.headers.get("user-agent")
    )
    
    discord_invite = "https://discord.gg/your-invite-code"
    return RedirectResponse(url=discord_invite, status_code=302)

# Additional optimization: serve WebP images
class OptimizedStaticFiles(StaticFiles):
    """Serve WebP images when supported."""
    
    async def get_response(self, path: str, scope):
        # Check if browser supports WebP
        headers = dict(scope["headers"])
        accept = headers.get(b"accept", b"").decode()
        
        if "image/webp" in accept and path.endswith((".jpg", ".png")):
            # Try WebP version first
            webp_path = path.rsplit(".", 1)[0] + ".webp"
            try:
                return await super().get_response(webp_path, scope)
            except:
                pass  # Fall back to original
        
        return await super().get_response(path, scope)
```

## Task 7: Create Tests

### tests/test_landing_page.py

Test the landing page:

```python
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_landing_page_loads(test_client: AsyncClient):
    """Test landing page loads successfully."""
    response = await test_client.get("/")
    
    assert response.status_code == 200
    assert "Smarter Dev" in response.text
    assert "Learn. Code. Grow." in response.text

@pytest.mark.asyncio
async def test_landing_page_performance_headers(test_client: AsyncClient):
    """Test performance and security headers."""
    response = await test_client.get("/")
    
    assert "X-Content-Type-Options" in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "X-Frame-Options" in response.headers

@pytest.mark.asyncio
async def test_discord_redirect(test_client: AsyncClient):
    """Test Discord invite redirect."""
    response = await test_client.get("/discord", follow_redirects=False)
    
    assert response.status_code == 302
    assert "discord.gg" in response.headers["location"]

@pytest.mark.asyncio
async def test_static_assets(test_client: AsyncClient):
    """Test static assets are served."""
    # CSS
    css_response = await test_client.get("/static/css/landing.css")
    assert css_response.status_code == 200
    assert "text/css" in css_response.headers["content-type"]
    
    # JavaScript
    js_response = await test_client.get("/static/js/landing.js")
    assert js_response.status_code == 200
    assert "javascript" in js_response.headers["content-type"]

@pytest.mark.asyncio
async def test_mobile_responsive(test_client: AsyncClient):
    """Test mobile responsive elements exist."""
    response = await test_client.get("/")
    
    assert "mobile-menu-toggle" in response.text
    assert "viewport" in response.text
    assert "clamp(" in response.text  # Responsive font sizes
```

## Deliverables

1. **Landing Page HTML**
   - Modern, semantic structure
   - SEO optimized
   - Performance focused
   - Accessibility considered

2. **CSS Styling**
   - Custom properties for theming
   - Responsive design
   - Smooth animations
   - Optimized for performance

3. **Interactive JavaScript**
   - 3D code window effect
   - Scroll animations
   - Mobile menu
   - Progressive enhancement

4. **Animation System**
   - Reusable animation classes
   - Scroll-triggered animations
   - Reduced motion support
   - Performance optimized

5. **Static Assets**
   - SVG favicon
   - Optimized images
   - Component library
   - WebP support

6. **Performance**
   - Asset versioning
   - Caching headers
   - Font preloading
   - Lazy loading ready

## Important Notes

1. Mobile-first responsive design
2. Animations respect prefers-reduced-motion
3. All interactive elements keyboard accessible
4. Fast initial paint with critical CSS
5. Progressive enhancement approach
6. SEO friendly with proper meta tags

This modern landing page creates an engaging first impression while maintaining excellent performance and accessibility.