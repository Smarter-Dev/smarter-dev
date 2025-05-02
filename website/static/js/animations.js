document.addEventListener('DOMContentLoaded', function() {
    // Navbar scroll effect
    const navbar = document.querySelector('.navbar');

    const handleScroll = () => {
        if (window.scrollY > 50) {
            navbar.style.backgroundColor = 'rgba(26, 26, 26, 0.7)';
            navbar.style.backdropFilter = 'blur(15px)';
            navbar.style.webkitBackdropFilter = 'blur(15px)';
            navbar.style.boxShadow = '0 2px 10px rgba(0, 0, 0, 0.3), 0 0 20px rgba(59, 130, 246, 0.1)';
        } else {
            navbar.style.backgroundColor = 'rgba(26, 26, 26, 0.5)';
            navbar.style.backdropFilter = 'blur(12px)';
            navbar.style.webkitBackdropFilter = 'blur(12px)';
            navbar.style.boxShadow = '0 1px 3px rgba(0, 0, 0, 0.2)';
        }
    };

    window.addEventListener('scroll', handleScroll);
    handleScroll(); // Run once on load
    // Animate elements when they come into view
    const animateOnScroll = function() {
        const elements = document.querySelectorAll('.feature-card, .coming-soon-item, .hero-image, .hero-content');

        elements.forEach(element => {
            const elementPosition = element.getBoundingClientRect().top;
            const screenPosition = window.innerHeight / 1.2;

            if (elementPosition < screenPosition) {
                element.style.opacity = '1';
                element.style.transform = element.classList.contains('hero-image')
                    ? 'perspective(1000px) rotateY(-5deg) rotateX(5deg)'
                    : 'translateY(0)';
            }
        });
    };

    // Set initial styles
    const elementsToAnimate = document.querySelectorAll('.feature-card, .coming-soon-item');
    elementsToAnimate.forEach(element => {
        element.style.opacity = '0';
        element.style.transform = 'translateY(20px)';
        element.style.transition = 'opacity 0.6s ease, transform 0.6s ease';
    });

    // Hero section animations
    const heroImage = document.querySelector('.hero-image');
    const heroContent = document.querySelector('.hero-content');

    if (heroImage) {
        heroImage.style.opacity = '0';
        heroImage.style.transform = 'translateX(50px)';
        heroImage.style.transition = 'opacity 0.8s ease, transform 0.8s ease';

        setTimeout(() => {
            heroImage.style.opacity = '1';
            heroImage.style.transform = 'perspective(1000px) rotateY(-5deg) rotateX(5deg)';
        }, 300);
    }

    if (heroContent) {
        heroContent.style.opacity = '0';
        heroContent.style.transform = 'translateX(-50px)';
        heroContent.style.transition = 'opacity 0.8s ease, transform 0.8s ease';

        setTimeout(() => {
            heroContent.style.opacity = '1';
            heroContent.style.transform = 'translateX(0)';
        }, 300);
    }

    // Run on scroll
    window.addEventListener('scroll', animateOnScroll);

    // Run once on load
    animateOnScroll();

    // Typing animation for code
    const codeElement = document.querySelector('.code-content code');
    if (codeElement) {
        const originalText = codeElement.innerHTML;
        // Create a placeholder with the same structure but invisible text
        const placeholderText = originalText.replace(/[^\s<>\/?]/g, ' ');
        codeElement.innerHTML = placeholderText;

        let visibleText = '';
        let i = 0;
        const typeWriter = () => {
            if (i < originalText.length) {
                // Only replace characters, not HTML tags
                if (originalText.charAt(i) === '<') {
                    // Skip HTML tags
                    while (i < originalText.length && originalText.charAt(i) !== '>') {
                        visibleText += originalText.charAt(i);
                        i++;
                    }
                    if (i < originalText.length) {
                        visibleText += originalText.charAt(i); // Add the closing >
                    }
                } else {
                    visibleText += originalText.charAt(i);
                }
                i++;
                codeElement.innerHTML = visibleText;
                setTimeout(typeWriter, 20);
            }
        };

        // Start typing animation after a delay
        setTimeout(typeWriter, 1000);
    }

    // Smooth scrolling for anchor links
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function(e) {
            e.preventDefault();

            const targetId = this.getAttribute('href');
            if (targetId === '#') return;

            const targetElement = document.querySelector(targetId);
            if (targetElement) {
                targetElement.scrollIntoView({
                    behavior: 'smooth',
                    block: 'start'
                });
            }
        });
    });
});
