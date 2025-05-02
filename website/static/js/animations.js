document.addEventListener('DOMContentLoaded', function() {
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
        codeElement.innerHTML = '';
        
        let i = 0;
        const typeWriter = () => {
            if (i < originalText.length) {
                codeElement.innerHTML += originalText.charAt(i);
                i++;
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
