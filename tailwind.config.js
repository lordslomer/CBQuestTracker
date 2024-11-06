/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./templates/*'],
  plugins: [],
  theme: {
    extend: {
      fontFamily: {
        open: ['"Open Sans"'],
      },
      colors: {
        primary: {
          100: '#2e20e8',
          200: '#583aec',
          300: '#7553ef',
          400: '#8d6bf2',
          500: '#a383f5',
          600: '#b79bf8',
        },
        dark: {
          100: '#292929',
          200: '#3d3d3d',
          300: '#535353',
          400: '#696969',
          500: '#808080',
          600: '#989898',
        },
      },
    },
  },
};
